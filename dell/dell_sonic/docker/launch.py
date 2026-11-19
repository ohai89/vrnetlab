#!/usr/bin/env python3

import base64
import datetime
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import zlib
from pathlib import Path

import vrnetlab

CONFIG_FILE = "/config/config_db.json"
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "YourPaSsWoRd"
NTPSEC_HOOK = "/etc/network/if-up.d/ntpsec-ntpdate"
MGMT_READY_TIMEOUT = 300
# Bound on each console prompt wait after the readiness check.
PROMPT_TIMEOUT = 120
# Bound on waiting for hostcfgd to apply a CONFIG_DB hostname change.
HOSTCFGD_TIMEOUT = 20
# Bound on the shell prompt or "Login incorrect" after sending a password.
LOGIN_TIMEOUT = 60
# Wait for the guest's own login prompt after "Login incorrect" before
# nudging it with a carriage return.
LOGIN_PROMPT_WAIT = 10
ZTP_TIMEOUT = 180


def quiesce_ztp_cmd(timeout=ZTP_TIMEOUT):
    """Stop discovery before loading an explicit startup configuration.

    Match SONiC's ztp-profile.sh stop_ztp_dhcp: clear the discovery profile
    and synchronously regenerate interface configuration to stop DHCP workers.
    Stopping the ZTP service alone does not stop work run by interfaces-config.
    Do not load a factory fallback config or create a saved config here.
    """
    return (
        f"timeout -k 5 {timeout} sh -c '"
        "load=$(systemctl show -p LoadState --value ztp) || exit 1; "
        'case "$load" in not-found) exit 0;; loaded|masked) ;; *) exit 1;; esac; '
        "systemctl stop ztp && "
        '[ "$(systemctl show -p ActiveState --value ztp)" = inactive ] && '
        'sonic-db-cli CONFIG_DB DEL "ZTP|mode" >/dev/null && '
        "systemctl restart interfaces-config' "
        "&& printf '%s_%s\\n' ZTP QUIESCED || printf '%s_%s\\n' ZTP FAILED"
    )


def ntpsec_hook_cmd(hook=NTPSEC_HOOK):
    """Disable the ntpsec-ntpdate if-up hook and unblock the boot waiting on it.

    On SONiC 4.6 the hook runs "systemctl start ntpsec.service" from ifup and
    waits for it. ntpsec.service is ordered after units that wait for
    networking.service, so networking never finishes: the boot stalls with
    eth0, hostcfgd and multi-user.target never started, and every later boot
    hangs before the login prompt. Removing the execute bit persists on the
    disk; killing the waiting systemctl client lets ifup, and the boot,
    finish. Images without the hook, or where it is already disabled, are
    left untouched. The printf arguments keep the echoed command from
    matching the marker.
    """
    return (
        f"if [ -x {hook} ] && chmod -x {hook}; then "
        "pkill -x -f 'systemctl start ntpsec.service'; "
        "printf '%s_%s\\n' NTPSEC_HOOK DISABLED; "
        "else printf '%s_%s\\n' NTPSEC_HOOK UNCHANGED; fi"
    )


def mgmt_ready_cmd(timeout=MGMT_READY_TIMEOUT):
    """Poll until eth0 is up, sshd listens on IPv4 and CONFIG_DB is initialized.

    The console login can appear while config-setup is still reloading
    CONFIG_DB; "config load" then fails its PORT table check. Wait for the
    config-setup oneshot to finish and CONFIG_DB_INITIALIZED, the flag SONiC's
    own swss/syncd start scripts wait on. Also wait for the boot-time
    hostname-config oneshot, which otherwise races the hostname step.
    Finally wait until the bgp container reports up in STATE_DB. Its config
    daemon renders the routing config from CONFIG_DB when the container
    starts and then follows changes once initialized; a config load in
    between is not applied to the routing stack.
    The printf arguments keep the echoed command from matching the marker.
    """
    checks = (
        'ip link show eth0 | grep -q "state UP"'
        ' && ss -Hltn "sport = :22" | grep -q "0.0.0.0:22"'
        " && systemctl is-active --quiet config-setup"
        ' && [ "$(sonic-db-cli CONFIG_DB GET CONFIG_DB_INITIALIZED)" = 1 ]'
        " && ! systemctl list-jobs --no-legend | grep -q hostname-config"
        ' && [ "$(systemctl show -p ActiveState --value hostname-config)" != activating ]'
        ' && [ "$(sonic-db-cli STATE_DB HGET "FEATURE|bgp" UP_STATUS)" = TRUE ]'
    )
    return (
        f"timeout {timeout} sh -c 'until {checks}; do sleep 1; done'"
        " && printf '%s_%s\\n' MGMT READY || printf '%s_%s\\n' MGMT TIMEOUT"
    )


def valid_hostname(hostname):
    """SONiC's own hostname rule (config hostname/hostcfgd).

    Also guarantees the name is safe to embed in a shell command.
    """
    if not isinstance(hostname, str) or not hostname or len(hostname) > 253:
        return False
    label = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
    return all(label.fullmatch(x) for x in hostname.split("."))


def hostname_cmd(
    hostname,
    hostname_file="/etc/hostname",
    hosts_file="/etc/hosts",
    config_db="/etc/sonic/config_db.json",
    timeout=HOSTCFGD_TIMEOUT,
):
    """Set the hostname in CONFIG_DB and apply it to the live system.

    Mirrors SONiC's hostname-config.sh/hostcfgd (/etc/hostname, hostname -F,
    a "127.0.0.1 <name>" hosts entry). hostname-config runs once at boot (the
    readiness wait orders us after it) and hostcfgd may not be running yet.
    Where hostcfgd runs, it applies the CONFIG_DB change only if it has
    subscribed and its cached hostname differs; a write that lands while it
    initializes is cached and never applied. So wait (bounded) for hostcfgd
    to apply it, and apply the live steps only if it did not, so its own
    hosts entry is not duplicated. Writing CONFIG_DB first means SONiC agrees
    with the live hostname instead of resetting it later. The saved config is
    only updated if it exists; it is not created here.
    """
    if not valid_hostname(hostname):
        raise ValueError(f"invalid hostname {hostname!r}")
    tmp = f"{hosts_file}.vrnetlab"
    persist = (
        'python3 -c "import json,os,shutil,sys;p,h=sys.argv[1:];'
        "d=json.load(open(p));"
        "d.setdefault('DEVICE_METADATA',{}).setdefault('localhost',{})['hostname']=h;"
        "t=p+'.vrnetlab';json.dump(d,open(t,'w'),indent=4);"
        'shutil.copymode(p,t);os.replace(t,p)"'
    )
    return (
        f'H={hostname}; O="$(hostname)"; A=; '
        "sonic-db-cli CONFIG_DB HSET 'DEVICE_METADATA|localhost' hostname \"$H\" >/dev/null; "
        "if systemctl is-active --quiet hostcfgd; then i=0; "
        f'until [ "$(hostname)" = "$H" ] || [ $i -ge {timeout} ]; do '
        "sleep 1; i=$((i+1)); done; "
        '[ "$(hostname)" = "$H" ] && A=1; fi; '
        "if [ -n \"$A\" ]; then echo 'hostcfgd applied the hostname'; else "
        f'echo "$H" > {hostname_file} && hostname -F {hostname_file}; '
        'awk -v o="$O" -v h="$H" \'!($1 == "127.0.0.1" && NF == 2 && '
        '($2 == h || ($2 == o && o != "localhost")))\' '
        f'{hosts_file} > {tmp} && echo "127.0.0.1 $H" >> {tmp} && '
        f"cat {tmp} > {hosts_file}; rm -f {tmp}; fi; "
        f'if [ -f {config_db} ]; then {persist} {config_db} "$H"; '
        f"else echo '{config_db} absent, hostname not persisted to it'; fi"
    )


def handle_SIGCHLD(_signal, _frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_signal, _frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class Dell_Sonic_VM(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = vrnetlab.find_disk_image()
        super(Dell_Sonic_VM, self).__init__(
            username, password, disk_image=disk_image, ram=4096, smp="2"
        )
        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = 10
        self.hostname = hostname
        self.nic_identity = {}
        self.nic_identity_reboots = 0

    def remember_nics(self, args):
        """Record the exact MACs emitted to QEMU, including snapshot/dummy NICs."""
        for i, arg in enumerate(args[:-1]):
            if arg != "-device":
                continue
            fields = dict(
                field.split("=", 1) for field in args[i + 1].split(",") if "=" in field
            )
            match = re.fullmatch(r"p([0-9]+)", fields.get("netdev", ""))
            if not match:
                continue
            index = int(match.group(1))
            mac = fields.get("mac")
            if not mac:
                # Sparse topologies still need stable identities for gap NICs.
                mac = vrnetlab.gen_mac(index)
                args[i + 1] += f",mac={mac}"
            self.nic_identity[f"eth{index}"] = mac.lower()
        return args

    def gen_mgmt(self):
        return self.remember_nics(super().gen_mgmt())

    def gen_nics(self):
        return self.remember_nics(super().gen_nics())

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            if self.nic_identity_reboots:
                self.logger.error(
                    "NIC identity boot did not reach login; aborting startup"
                )
                sys.exit(1)
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        ridx, match, res = self.tn.expect([b"login:"], 1)
        if match and ridx == 0:  # login
            self.logger.info("VM started")

            self.console_login()

            if not self.ensure_nic_identity():
                return

            # run main config!
            self.bootstrap_config()
            self.startup_config()
            # close telnet connection
            self.tn.close()
            # startup time?
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info(f"Startup complete in: {startup_time}")
            # mark as running
            self.running = True
            return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def ensure_nic_identity(self):
        """Install early-boot identity assignment, never repair live wiring."""
        if "eth0" not in self.nic_identity:
            self.logger.error("No QEMU management NIC identity; aborting startup")
            sys.exit(1)
        self.wait_write("sudo -i", "$")
        self.disable_ntpsec_hook()
        encoded = base64.b64encode(
            zlib.compress(Path(__file__).with_name("nic_identity.py").read_bytes())
        ).decode()
        self.prompt_write("VRL_NIC_PAYLOAD=")
        # Keep each console line well below the guest's canonical TTY limit.
        for offset in range(0, len(encoded), 768):
            self.prompt_write(
                f"VRL_NIC_PAYLOAD=$VRL_NIC_PAYLOAD'{encoded[offset : offset + 768]}'"
            )
        decode = (
            "import base64,sys,zlib;"
            "open('/run/vrnetlab-nic-bootstrap.py','wb').write("
            "zlib.decompress(base64.b64decode(sys.argv[1])))"
        )
        self.prompt_write(
            f'python3 -c {shlex.quote(decode)} "$VRL_NIC_PAYLOAD" && '
            "python3 /run/vrnetlab-nic-bootstrap.py install "
            + shlex.quote(json.dumps(self.nic_identity, sort_keys=True))
        )
        ridx, match, buf = self.tn.expect(
            [b"NIC_IDENTITY_READY", b"NIC_IDENTITY_INSTALLED", b"NIC_IDENTITY_FAILED"],
            PROMPT_TIMEOUT,
        )
        if match and "#" in buf.decode(errors="ignore")[match.end() :]:
            self.wait_write("", wait=None)
        if not match or ridx == 2:
            self.logger.error(
                "Could not establish boot-time NIC identity; aborting startup"
            )
            sys.exit(1)
        if ridx == 0:
            self.logger.info("Boot-time NIC identities match QEMU MAC assignments")
            self.prompt_write("exit")
            return True
        if self.nic_identity_reboots:
            self.logger.error(
                "NIC identity marker missing after reboot; aborting startup"
            )
            sys.exit(1)
        self.nic_identity_reboots += 1
        self.logger.info(
            "Installed early NIC identity service; rebooting before config restore"
        )
        self.prompt_write("systemctl reboot")
        self.spins = 0
        return False

    def console_login(self):
        """Log in on the serial console.

        A fresh disk has the factory password. A disk reused across a
        container restart already has the configured password, so retry
        with it after "Login incorrect". Exit if neither works.
        """
        passwords = [DEFAULT_PASSWORD]
        if self.password != DEFAULT_PASSWORD:
            passwords.append(self.password)
        prompt = f"{DEFAULT_USER}@".encode()
        self.wait_write("\r", None)
        self.wait_write(DEFAULT_USER, wait="login:")
        for i, password in enumerate(passwords):
            if i:
                self.logger.info("Console login incorrect, trying the next password")
                # Type nothing ahead: wait for the guest's own login prompt.
                if b"login:" not in rest:
                    rest = self.tn.read_until(b"login:", LOGIN_PROMPT_WAIT)
                if b"login:" not in rest:
                    # login(1) can sit silently waiting for input until its
                    # own timeout; an empty line makes it prompt again.
                    self.logger.info("No login prompt, sending a carriage return")
                    self.wait_write("", wait=None)
                    rest = self.tn.read_until(b"login:", LOGIN_TIMEOUT)
                    if b"login:" not in rest:
                        break
                self.wait_write(DEFAULT_USER, wait=None)
            self.wait_write(password, wait="Password:")
            ridx, match, buf = self.tn.expect(
                [prompt, b"Login incorrect"], LOGIN_TIMEOUT
            )
            if match and ridx == 0:
                self.wait_write("", None)
                self.logger.info("Login completed")
                return
            if not match:
                break
            rest = buf.decode(errors="ignore")[match.end() :].encode()
        self.logger.error("Console login failed, aborting startup")
        sys.exit(1)

    def bootstrap_config(self):
        """Do the actual bootstrap config"""
        self.logger.info("applying bootstrap configuration")
        self.wait_write("sudo -i", "$")

        self.disable_ntpsec_hook()
        self.bootstrap_mgmt()
        self.wait_write("passwd -q %s" % (self.username))
        self.wait_write(self.password, "New password:")
        self.wait_write(self.password, "password:")
        self.wait_write("sleep 1", "#")
        self.wait_mgmt_ready()
        if os.path.exists(CONFIG_FILE):
            self.quiesce_ztp()
            # Discovery cleanup may have reconfigured management as well.
            self.bootstrap_mgmt()
        self.bootstrap_hostname()
        self.prompt_write("logout")
        self.logger.info("completed bootstrap configuration")

    def disable_ntpsec_hook(self):
        """Break the SONiC 4.6 boot deadlock on the ntpsec-ntpdate hook."""
        self.wait_write(ntpsec_hook_cmd(), "#")
        ridx, match, buf = self.tn.expect(
            [b"NTPSEC_HOOK_DISABLED", b"NTPSEC_HOOK_UNCHANGED"], PROMPT_TIMEOUT
        )
        # As in wait_mgmt_ready: re-elicit a prompt that expect() consumed.
        if "#" in buf.decode(errors="ignore")[match.end() if match else 0 :]:
            self.wait_write("", wait=None)
        if match and ridx == 0:
            self.logger.info(f"Disabled {NTPSEC_HOOK}, which deadlocks the guest boot")
        elif not match:
            self.logger.warning(f"Could not check {NTPSEC_HOOK}, continuing")

    def quiesce_ztp(self):
        """Fail closed if provisioning cannot be stopped before config restore."""
        self.prompt_write(quiesce_ztp_cmd())
        ridx, match, buf = self.tn.expect(
            [b"ZTP_QUIESCED", b"ZTP_FAILED"], ZTP_TIMEOUT + 30
        )
        if match and "#" in buf.decode(errors="ignore")[match.end() :]:
            self.wait_write("", wait=None)
        if not match or ridx != 0:
            self.logger.error("Could not quiesce ZTP discovery; aborting startup")
            sys.exit(1)
        self.logger.info("ZTP discovery quiesced before startup configuration restore")

    def bootstrap_mgmt(self):
        """Bring up management (address, link, default route).

        SONiC's own path (interfaces-config/hostcfgd) may not have configured
        eth0 by the time the console login appears, so apply the wrapper's
        management settings directly. Nothing is written to CONFIG_DB, so a
        startup config loaded later keeps its own MGMT_* settings.
        """
        if self.mgmt_address_ipv4 != "dhcp":
            self.wait_write(
                f"ip address replace {self.mgmt_address_ipv4} dev eth0", "#"
            )
        if self.mgmt_address_ipv6 and self.mgmt_address_ipv6 != "dhcp":
            # note, v6 address is not being applied for whatever reason
            self.wait_write(
                f"ip -6 address replace {self.mgmt_address_ipv6} dev eth0", "#"
            )
        self.wait_write("ip link set eth0 up", "#")
        if self.mgmt_gw_ipv4 and self.mgmt_gw_ipv4 != "dhcp":
            self.wait_write(
                f"ip route replace default via {self.mgmt_gw_ipv4} dev eth0", "#"
            )
        if self.mgmt_gw_ipv6 and self.mgmt_gw_ipv6 != "dhcp":
            self.wait_write(
                f"ip -6 route replace default via {self.mgmt_gw_ipv6} dev eth0", "#"
            )

    def wait_mgmt_ready(self):
        """Wait for the guest state the SSH-based startup loader depends on."""
        self.logger.info(
            "waiting for management interface, IPv4 SSH, CONFIG_DB initialization "
            "and the bgp container"
        )
        self.wait_write(mgmt_ready_cmd(), "#")
        ridx, match, buf = self.tn.expect(
            [b"MGMT_READY", b"MGMT_TIMEOUT"], MGMT_READY_TIMEOUT + 30
        )
        # expect() drops the rest of the chunk it matched in. If the shell
        # prompt arrived in that chunk it is gone; ask for a fresh one so the
        # next prompt wait sees exactly one prompt either way.
        if match and "#" in buf.decode(errors="ignore")[match.end() :]:
            self.wait_write("", wait=None)
        if match and ridx == 0:
            self.logger.info(
                "management interface, IPv4 SSH, CONFIG_DB and the bgp container "
                "are ready"
            )
        else:
            self.logger.warning(
                "management interface, IPv4 SSH, CONFIG_DB or the bgp container "
                "not ready after "
                f"{MGMT_READY_TIMEOUT}s; SSH-based startup config loading may fail"
            )

    def intended_hostname(self):
        """Hostname from the startup config if it sets one, else the node name.

        Returns None if the chosen name is not a valid SONiC hostname, so an
        intentional startup-config hostname is never silently replaced.
        """
        hostname, source = self.hostname, "node name"
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f:
                    meta = json.load(f).get("DEVICE_METADATA", {}).get("localhost", {})
                configured = meta.get("hostname")
            except (OSError, ValueError, AttributeError) as e:
                self.logger.warning(
                    f"Cannot read hostname from {CONFIG_FILE} ({type(e).__name__}), "
                    "using the node name"
                )
                configured = None
            if configured is not None:
                hostname, source = configured, f"startup config {CONFIG_FILE}"
        if not valid_hostname(hostname):
            self.logger.warning(
                f"Invalid hostname {hostname!r} from {source}, not setting hostname"
            )
            return None
        self.logger.info(f"Using hostname {hostname} from {source}")
        return hostname

    def bootstrap_hostname(self):
        """Apply the intended hostname to CONFIG_DB and the live system.

        Runs after CONFIG_DB initialization, so config-setup's reload cannot
        reset it; a startup config loaded later sets the same hostname.
        """
        hostname = self.intended_hostname()
        if hostname:
            self.prompt_write(hostname_cmd(hostname))

    def prompt_write(self, cmd, timeout=PROMPT_TIMEOUT):
        """Wait (bounded) for the root shell prompt, then send cmd.

        A lost prompt fails startup instead of leaving it hanging forever.
        """
        res = self.tn.read_until(b"#", timeout)
        if b"#" not in res:
            self.logger.error(
                f"No shell prompt on the serial console within {timeout}s, "
                "aborting startup"
            )
            sys.exit(1)
        self.wait_write(cmd, wait=None)

    def startup_config(self):
        """Load additional config provided by user."""

        if not os.path.exists(CONFIG_FILE):
            self.logger.trace(
                f"Startup config file {CONFIG_FILE} is not provided, nothing to do"
            )
            return

        self.logger.trace(
            f"Startup config file {CONFIG_FILE} found, copying it to the VM"
        )

        # handle_SIGCHLD would reap the child before subprocess collects its
        # exit status, which makes a failed restore look successful
        previous_sigchld = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        try:
            subprocess.run(
                ["/backup.sh", "-u", self.username, "-p", self.password, "restore"],
                check=True,
                timeout=600,
            )
        except (subprocess.SubprocessError, OSError):
            self.logger.error(
                f"Failed to apply startup config {CONFIG_FILE}; aborting startup"
            )
            sys.exit(1)
        finally:
            signal.signal(signal.SIGCHLD, previous_sigchld)


class Dell_SONiC(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super().__init__(username, password)
        self.vms = [Dell_Sonic_VM(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="sonic", help="SONiC hostname")
    parser.add_argument("--username", default="admin", help="Username")
    parser.add_argument("--password", default="admin", help="Password")
    parser.add_argument(
        "--connection-mode", default="tc", help="Connection mode to use in the datapath"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = Dell_SONiC(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    vr.start()
