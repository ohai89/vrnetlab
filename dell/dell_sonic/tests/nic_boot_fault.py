"""Opt-in guest-only boot fault injection. Never part of the production image.

Run as root inside a disposable SONiC VM with `install <JSON identity map>`,
then reboot. Only the two supplied dataplane NICs are unbound/rebound. Unit
ordering guarantees injection precedes the identity service and networking.
No live repair is performed. Remove by discarding the disposable guest disk.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DIRECTORY = Path("/usr/local/lib/vrnetlab")
CONFIG = Path("/etc/vrnetlab/nic-boot-fault.json")
RECORD = Path("/run/vrnetlab-nic-boot-fault.json")
SERVICE = "vrnetlab-nic-boot-fault.service"
UNIT = """[Unit]
Description=Disposable test: reverse dataplane NIC registration before identity assignment
DefaultDependencies=no
Wants=systemd-udev-settle.service
After=systemd-udev-settle.service systemd-remount-fs.service
Before=vrnetlab-nic-identity.service sysinit.target networking.service sonic.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/vrnetlab/nic_boot_fault.py apply
RemainAfterExit=yes
TimeoutStartSec=30
"""


def snapshot():
    result = {}
    for path in Path("/sys/class/net").iterdir():
        if not (path / "device").exists():
            continue
        result[path.name] = {
            "mac": (path / "address").read_text().strip().lower(),
            "device": str((path / "device").resolve()),
            "flags": int((path / "flags").read_text().strip(), 16),
        }
    return result


def inject():
    mapping = json.loads(CONFIG.read_text())
    before = snapshot()
    if before["eth0"]["mac"] != mapping["eth0"]:
        raise ValueError(
            "Management already has an unexpected identity; preserving evidence"
        )
    devices = []
    for target in ("eth1", "eth2"):
        matches = [d for d in before.values() if d["mac"] == mapping[target]]
        if len(matches) != 1 or matches[0]["flags"] & 1:
            raise ValueError("Expected exactly one inactive dataplane NIC")
        device = Path(matches[0]["device"])
        if (device / "driver").resolve().name != "virtio_net":
            raise ValueError("Fault injection requires virtio dataplane NICs")
        devices.append(device.name)
    driver = Path("/sys/bus/virtio/drivers/virtio_net")
    for device in devices:
        (driver / "unbind").write_text(device)
    for device in reversed(devices):
        (driver / "bind").write_text(device)
    after = snapshot()
    if (
        after["eth1"]["mac"] != mapping["eth2"]
        or after["eth2"]["mac"] != mapping["eth1"]
    ):
        raise ValueError("Injection did not reproduce the intended name reversal")
    # Negative test: make identity ambiguous without altering firmware intent.
    # Only enabled by an explicit disposable-guest service environment override.
    duplicate = os.environ.get("VRNETLAB_NIC_FAULT") == "duplicate"
    if duplicate:
        subprocess.run(
            ["ip", "link", "set", "dev", "eth2", "address", mapping["eth2"]],
            check=True,
        )
        after = snapshot()
    RECORD.write_text(
        json.dumps(
            {
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "monotonic": time.monotonic(),
                "duplicate": duplicate,
                "before": before,
                "after": after,
            },
            indent=2,
        )
        + "\n"
    )
    print("NIC_BOOT_FAULT_REPRODUCED", flush=True)


def install(mapping):
    if (
        set(mapping) != {"eth0", "eth1", "eth2"}
        or any(
            not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value)
            for value in mapping.values()
        )
        or len(set(mapping.values())) != 3
    ):
        raise ValueError("Boot fault requires exactly three distinct intended MACs")
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    (DIRECTORY / "nic_boot_fault.py").write_text(Path(__file__).read_text())
    CONFIG.write_text(json.dumps(mapping) + "\n")
    units = Path("/etc/systemd/system")
    (units / SERVICE).write_text(UNIT)
    required = units / "sysinit.target.requires" / SERVICE
    required.parent.mkdir(parents=True, exist_ok=True)
    if not required.is_symlink():
        required.symlink_to("../" + SERVICE)
    elif os.readlink(required) != "../" + SERVICE:
        raise ValueError("Existing fault dependency points elsewhere")
    for consumer in ("networking.service", "sonic.target"):
        dropin = units / (consumer + ".d")
        dropin.mkdir(parents=True, exist_ok=True)
        (dropin / "05-vrnetlab-nic-boot-fault.conf").write_text(
            f"[Unit]\nRequires={SERVICE}\nAfter={SERVICE}\n"
        )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("NIC_BOOT_FAULT_INSTALLED: reboot required; no NICs changed", flush=True)


def report():
    """Report failure gating on the serial console when SSH cannot start."""
    units = {}
    for name in (
        "vrnetlab-nic-identity.service",
        "networking.service",
        "sonic.target",
        "config-setup.service",
        "swss.service",
        "syncd.service",
    ):
        output = subprocess.check_output(
            [
                "systemctl",
                "show",
                name,
                "-p",
                "ActiveState",
                "-p",
                "ActiveEnterTimestampMonotonic",
                "-p",
                "InactiveExitTimestampMonotonic",
            ],
            text=True,
        )
        units[name] = dict(line.split("=", 1) for line in output.splitlines())
    evidence = {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "marker_exists": Path("/run/vrnetlab-nic-identity.json").exists(),
        "units": units,
        "fault": json.loads(RECORD.read_text()),
    }
    print("NIC_FAILCLOSED " + json.dumps(evidence, sort_keys=True), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["report"]:
        report()
    elif sys.argv[1:] == ["apply"]:
        inject()
    elif len(sys.argv) == 3 and sys.argv[1] == "install":
        install(json.loads(sys.argv[2]))
    else:
        raise SystemExit(
            "Use apply or install <JSON identity map> inside a disposable guest"
        )
