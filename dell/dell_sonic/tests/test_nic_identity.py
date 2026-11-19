"""Exercise early-boot identity assignment, including swapped kernel names."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
