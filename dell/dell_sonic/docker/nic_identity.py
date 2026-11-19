"""Assign launcher-specified NIC identities before SONiC/networking starts.

Installed on the guest overlay, never in the supplied image. The installer does
not rename anything: a reboot is mandatory before the launcher restores config.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SERVICE = "vrnetlab-nic-identity.service"
HELPER = Path("/usr/local/lib/vrnetlab/nic_identity.py")
CONFIG = Path("/etc/vrnetlab/nic-identity.json")
MARKER = Path("/run/vrnetlab-nic-identity.json")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
NET = Path("/sys/class/net")
UNIT = """# Managed by vrnetlab NIC identity bootstrap
[Unit]
Description=Assign physical NIC identities before SONiC
DefaultDependencies=no
Wants=systemd-udev-settle.service
After=systemd-udev-settle.service systemd-remount-fs.service
Before=sysinit.target network-pre.target networking.service sonic.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/vrnetlab/nic_identity.py apply
RemainAfterExit=yes
TimeoutStartSec=45
"""
GATE = """# Managed by vrnetlab NIC identity bootstrap
[Unit]
Requires=vrnetlab-nic-identity.service
After=vrnetlab-nic-identity.service
"""


def validate(mapping):
    if not isinstance(mapping, dict) or "eth0" not in mapping:
        raise ValueError("NIC intent must include management eth0")
    for name, mac in mapping.items():
        if not re.fullmatch(r"eth[0-9]{1,3}", name) or not isinstance(mac, str):
            raise ValueError("Invalid NIC identity")
        if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac):
            raise ValueError("Invalid NIC MAC address")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Duplicate intended NIC MAC address")


def resolve(mapping, net=NET):
    validate(mapping)
    found = {}
    for path in net.iterdir():
        if not (path / "device").exists():
            continue
        mac = (path / "address").read_text().strip().lower()
        if mac in mapping.values():
            if mac in found:
                raise ValueError("Ambiguous physical NIC MAC address")
            found[mac] = path.name
    if set(found) != set(mapping.values()):
        raise ValueError("Missing intended physical NIC")
    for target in mapping:
        if (net / target).exists() and target not in found.values():
            raise ValueError("NIC name occupied by an unintended device")
    return {target: found[mac] for target, mac in mapping.items()}


def verify(mapping, net=NET):
    if any(target != current for target, current in resolve(mapping, net).items()):
        raise ValueError("Physical NIC identities do not match launcher intent")


def apply(mapping, net=NET):
    """Preflight everything, then free all conflicting names before assigning."""
    resolved = resolve(mapping, net)
    changes = []
    for i, target in enumerate(sorted(mapping, key=lambda n: int(n[3:]))):
        current = resolved[target]
        if current == target:
            continue
        temporary = f"vrl{i}"
        if (net / temporary).exists():
            raise ValueError("Temporary NIC name already exists")
        if int((net / current / "flags").read_text().strip(), 16) & 1:
            raise ValueError("Refusing to rename an active NIC")
        changes.append((current, temporary, target))
    for current, temporary, _ in changes:
        subprocess.run(
            ["ip", "link", "set", "dev", current, "name", temporary], check=True
        )
    for _, temporary, target in changes:
        subprocess.run(
            ["ip", "link", "set", "dev", temporary, "name", target], check=True
        )
    verify(mapping, net)


def marker_matches(marker, digest, boot_id):
    try:
        data = json.loads(marker.read_text())
        return data.get("digest") == digest and data.get("boot_id") == boot_id
    except (OSError, ValueError, AttributeError):
        return False


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def install(mapping):
    validate(mapping)
    source = Path(__file__).read_text()
    digest = hashlib.sha256(
        (source + json.dumps(mapping, sort_keys=True)).encode()
    ).hexdigest()
    if marker_matches(MARKER, digest, BOOT_ID.read_text().strip()):
        verify(mapping)
        print("NIC_IDENTITY_READY", flush=True)
        return
    # Do not start the service on this boot. Consumers may already be running.
    write(HELPER, source)
    write(CONFIG, json.dumps({"mapping": mapping, "digest": digest}) + "\n")
    unit_dir = Path("/etc/systemd/system")
    write(unit_dir / SERVICE, UNIT)
    for consumer in ("networking.service", "sonic.target"):
        write(unit_dir / f"{consumer}.d/10-vrnetlab-nic-identity.conf", GATE)
    required = unit_dir / "sysinit.target.requires" / SERVICE
    required.parent.mkdir(parents=True, exist_ok=True)
    if required.is_symlink():
        if os.readlink(required) != "../" + SERVICE:
            raise ValueError("NIC service dependency already points elsewhere")
    else:
        required.symlink_to("../" + SERVICE)
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
    print("NIC_IDENTITY_INSTALLED", flush=True)


def boot():
    config = json.loads(CONFIG.read_text())
    mapping = config["mapping"]
    validate(mapping)
    apply(mapping)
    evidence = {
        "digest": config["digest"],
        "boot_id": BOOT_ID.read_text().strip(),
        "monotonic": time.monotonic(),
        "mapping": mapping,
    }
    write(MARKER, json.dumps(evidence) + "\n")
    print("NIC_IDENTITY_APPLIED " + json.dumps(mapping, sort_keys=True), flush=True)


def main():
    try:
        if sys.argv[1:] == ["apply"]:
            boot()
        elif len(sys.argv) == 3 and sys.argv[1] == "install":
            install(json.loads(sys.argv[2]))
        else:
            raise ValueError("Invalid NIC identity command")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        # No shell commands, credentials or supplied startup config in errors.
        print(f"NIC_IDENTITY_FAILED: {type(error).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
