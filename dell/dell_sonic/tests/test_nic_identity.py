"""Exercise early-boot identity assignment, including swapped kernel names."""

import importlib.util
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_management_bootstrap import launch

SPEC = importlib.util.spec_from_file_location(
    "nic_identity",
    Path(__file__).resolve().parents[1] / "docker" / "nic_identity.py",
)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)

MAP = {
    "eth0": "02:00:00:00:00:10",
    "eth1": "02:00:00:00:00:11",
    "eth2": "02:00:00:00:00:12",
}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.net = self.root / "net"
        self.net.mkdir()
        self.calls = []

    def device(self, name, mac, flags=0):
        p = self.net / name
        p.mkdir()
        (p / "device").mkdir()
        (p / "address").write_text(mac + "\n")
        (p / "flags").write_text(hex(flags))

    def rename(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[:4], ["ip", "link", "set", "dev"])
        self.assertEqual(args[5], "name")
        self.assertFalse((self.net / args[6]).exists())
        (self.net / args[4]).rename(self.net / args[6])

    def apply(self, mapping=MAP):
        with patch.object(identity.subprocess, "run", side_effect=self.rename):
            identity.apply(mapping, net=self.net)

    def test_swapped_registration_uses_two_stages_and_preserves_management(self):
        self.device("eth0", MAP["eth0"])
        self.device("eth1", MAP["eth2"])
        self.device("eth2", MAP["eth1"])
        self.apply()
        identity.verify(MAP, net=self.net)
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(c[6].startswith("vrl") for c in self.calls[:2]))
        self.assertEqual([c[6] for c in self.calls[2:]], ["eth1", "eth2"])
        self.assertFalse(any(c[4] == "eth0" for c in self.calls))

    def test_management_can_also_be_in_a_cycle(self):
        for i in range(3):
            self.device(f"eth{i}", MAP[f"eth{(i + 1) % 3}"])
        self.apply()
        identity.verify(MAP, net=self.net)

    def test_every_three_nic_registration_permutation(self):
        for macs in itertools.permutations(MAP.values()):
            with self.subTest(macs=macs), tempfile.TemporaryDirectory() as directory:
                previous = self.net
                self.net = Path(directory)
                self.calls.clear()
                for name, mac in zip(MAP, macs, strict=True):
                    self.device(name, mac)
                self.apply()
                identity.verify(MAP, net=self.net)
                self.net = previous

    def test_already_correct_does_not_touch_links(self):
        for name, mac in MAP.items():
            self.device(name, mac, flags=1)
        self.apply()
        self.assertEqual(self.calls, [])

    def test_missing_duplicate_unrelated_or_up_device_fails_before_mutation(self):
        for case in ("missing", "duplicate", "unrelated", "up", "temporary"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                previous = self.net
                self.net = Path(directory)
                self.device("eth0", MAP["eth0"])
                self.device("eth1", MAP["eth2"], flags=1 if case == "up" else 0)
                if case != "missing":
                    self.device("eth2", MAP["eth1"])
                if case == "duplicate":
                    self.device("extra", MAP["eth1"])
                if case == "unrelated":
                    (self.net / "eth2").rename(self.net / "extra")
                    self.device("eth2", "02:00:00:00:00:99")
                if case == "temporary":
                    self.device("vrl1", "02:00:00:00:00:99")
                with self.assertRaises(ValueError):
                    self.apply()
                self.assertEqual(self.calls, [])
                self.net = previous

    def test_invalid_or_duplicate_intent_is_rejected(self):
        for mapping in (
            {},
            {"eth1;id": MAP["eth1"]},
            {"eth0": "invalid"},
            {"eth0": MAP["eth0"], "eth1": MAP["eth0"]},
        ):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                identity.validate(mapping)

    def test_marker_requires_current_boot_and_exact_intent(self):
        marker = self.root / "marker"
        marker.write_text(json.dumps({"digest": "old", "boot_id": "boot"}))
        self.assertFalse(identity.marker_matches(marker, "new", "boot"))
        self.assertFalse(identity.marker_matches(marker, "old", "other-boot"))
        self.assertTrue(identity.marker_matches(marker, "old", "boot"))

    def test_service_gates_sysinit_and_has_no_normal_service_dependency_cycle(self):
        self.assertIn("DefaultDependencies=no", identity.UNIT)
        self.assertIn("Before=sysinit.target", identity.UNIT)
        self.assertIn("After=systemd-udev-settle.service", identity.UNIT)
        self.assertIn("Requires=vrnetlab-nic-identity.service", identity.GATE)
        self.assertIn("After=vrnetlab-nic-identity.service", identity.GATE)

    def test_firmware_intent_is_read_afresh_for_each_vm_launch(self):
        firmware = self.root / "firmware"
        with (
            patch.object(identity, "FW_CFG", firmware),
            patch.object(identity.subprocess, "run"),
        ):
            firmware.write_text(json.dumps(MAP))
            self.assertEqual(identity.read_intent(), MAP)
            changed = dict(MAP, eth0="02:00:00:00:00:aa")
            firmware.write_text(json.dumps(changed))
            self.assertEqual(identity.read_intent(), changed)

    def test_missing_or_mismatched_firmware_cannot_install_or_mark_ready(self):
        firmware = self.root / "firmware"
        with (
            patch.object(identity, "FW_CFG", firmware),
            patch.object(identity.subprocess, "run"),
            patch.object(identity, "write") as write,
        ):
            with self.assertRaises(FileNotFoundError):
                identity.install(MAP)
            firmware.write_text(json.dumps(dict(MAP, eth0="02:00:00:00:00:aa")))
            with self.assertRaises(ValueError):
                identity.install(MAP)
            write.assert_not_called()

    def test_failed_assignment_never_creates_completion_marker(self):
        marker = self.root / "marker"
        with (
            patch.object(identity, "MARKER", marker),
            patch.object(identity, "read_intent", return_value=MAP),
            patch.object(identity.subprocess, "run"),
            patch.object(identity, "apply", side_effect=ValueError("ambiguous")),
            self.assertRaises(ValueError),
        ):
            identity.boot()
        self.assertFalse(marker.exists())


class LauncherIdentityTests(unittest.TestCase):
    def vm(self):
        vm = launch.Dell_Sonic_VM.__new__(launch.Dell_Sonic_VM)
        vm.nic_identity = {}
        return vm

    def test_records_emitted_macs_without_changing_provisioned_qemu_devices(self):
        vm = self.vm()
        args = [
            "-device",
            "virtio-net-pci,netdev=p00,mac=02:00:00:00:00:AA",
            "-netdev",
            "user,id=p00",
            "-device",
            "virtio-net-pci,netdev=p01,mac=02:00:00:00:00:BB,bus=pci.1,addr=0x2",
        ]
        original = args.copy()
        self.assertEqual(vm.remember_nics(args), original)
        self.assertEqual(
            vm.nic_identity, {"eth0": "02:00:00:00:00:aa", "eth1": "02:00:00:00:00:bb"}
        )

    def test_gap_nic_receives_an_explicit_mac_without_moving_pci_slot(self):
        vm = self.vm()
        args = ["-device", "virtio-net-pci,netdev=p02,bus=pci.1,addr=0x3"]
        with patch.object(
            launch.vrnetlab, "gen_mac", return_value=MAP["eth2"], create=True
        ):
            vm.remember_nics(args)
        self.assertEqual(
            args[1], "virtio-net-pci,netdev=p02,bus=pci.1,addr=0x3,mac=" + MAP["eth2"]
        )
        self.assertEqual(vm.nic_identity, {"eth2": MAP["eth2"]})

    def test_firmware_payload_matches_the_exact_generated_qemu_macs(self):
        vm = self.vm()
        vm.nic_identity = {"eth0": MAP["eth0"]}
        args = ["-device", "virtio-net-pci,netdev=p01,mac=" + MAP["eth1"]]
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "intent.json"
            with (
                patch.object(launch, "NIC_FW_CFG_FILE", str(payload), create=True),
                patch.object(
                    launch.vrnetlab.VM, "gen_nics", return_value=args, create=True
                ),
            ):
                result = vm.gen_nics()
            self.assertEqual(
                json.loads(payload.read_text()),
                {"eth0": MAP["eth0"], "eth1": MAP["eth1"]},
            )
            self.assertEqual(
                result[-2:],
                ["-fw_cfg", "name=opt/vrnetlab/nic-identity,file=" + str(payload)],
            )
            self.assertEqual(result[:2], args)

    def test_identity_reboot_is_bounded_and_never_uses_live_apply(self):
        import logging
        import re

        for marker, previous, expected in (
            (b"NIC_IDENTITY_READY", 1, True),
            (b"NIC_IDENTITY_INSTALLED", 0, False),
            (b"NIC_IDENTITY_INSTALLED", 1, "fail"),
            (b"NIC_IDENTITY_FAILED", 0, "fail"),
            (b"", 0, "fail"),
        ):
            with self.subTest(marker=marker, previous=previous):
                vm = self.vm()
                vm.nic_identity = MAP
                vm.nic_identity_reboots = previous
                vm.logger = logging.getLogger("nic_identity_test")
                vm.wait_write = MagicMock()
                vm.prompt_write = MagicMock()
                vm.disable_ntpsec_hook = MagicMock()
                vm.tn = MagicMock()
                index = (
                    [
                        b"NIC_IDENTITY_READY",
                        b"NIC_IDENTITY_INSTALLED",
                        b"NIC_IDENTITY_FAILED",
                    ].index(marker)
                    if marker
                    else -1
                )
                vm.tn.expect.return_value = (
                    index,
                    re.search(marker, marker) if marker else None,
                    marker,
                )
                if expected == "fail":
                    with (
                        self.assertRaises(SystemExit),
                        self.assertLogs(vm.logger, level="ERROR"),
                    ):
                        vm.ensure_nic_identity()
                else:
                    self.assertEqual(vm.ensure_nic_identity(), expected)
                commands = [c.args[0] for c in vm.prompt_write.call_args_list]
                self.assertFalse(
                    any("nic_identity.py apply" in cmd for cmd in commands)
                )
                self.assertFalse(any("ip link" in cmd for cmd in commands))
                self.assertTrue(all(len(cmd) < 4096 for cmd in commands))
