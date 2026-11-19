"""Lock down the direct-PCI NIC choice; cold boots validate guest ordering."""

import unittest
from unittest.mock import patch

from test_management_bootstrap import launch


class NICOrderTests(unittest.TestCase):
    def test_uses_direct_pci_driver_for_management_and_dataplane(self):
        with (
            patch.object(
                launch.vrnetlab,
                "find_disk_image",
                return_value="disk.qcow2",
                create=True,
            ),
            patch.object(launch.vrnetlab.VM, "__init__", return_value=None),
        ):
            vm = launch.Dell_Sonic_VM("sonic", "admin", "unused", "tc")
        self.assertEqual(vm.nic_type, "e1000")
