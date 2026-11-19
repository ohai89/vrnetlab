"""Exercise startup ordering and the real guest ZTP cleanup shell snippet."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_management_bootstrap import launch, make_vm


class ZTPTests(unittest.TestCase):
    def test_startup_config_quiesces_discovery_before_logout(self):
        vm = make_vm()
        vm.quiesce_ztp = MagicMock()
        vm.prompt_write = MagicMock()
        events = MagicMock()
        events.attach_mock(vm.quiesce_ztp, "quiesce")
        events.attach_mock(vm.prompt_write, "prompt")
        with patch.object(launch.os.path, "exists", return_value=True):
            vm.bootstrap_config()
        self.assertTrue(vm.quiesce_ztp.called)
        self.assertEqual(events.mock_calls[0][0], "quiesce")
        self.assertEqual(events.mock_calls[-1].args, ("logout",))

    def test_no_startup_config_leaves_ztp_alone(self):
        vm = make_vm()
        vm.quiesce_ztp = MagicMock()
        with patch.object(launch.os.path, "exists", return_value=False):
            vm.bootstrap_config()
        vm.quiesce_ztp.assert_not_called()

    def test_cleanup_failure_or_missing_marker_aborts(self):
        for marker in (b"ZTP_FAILED", b""):
            with self.subTest(marker=marker):
                vm = make_vm()
                match = re.search(marker, marker) if marker else None
                vm.tn.expect.return_value = (1, match, marker)
                with (
                    self.assertLogs(vm.logger, level="ERROR"),
                    self.assertRaises(SystemExit),
                ):
                    vm.quiesce_ztp()

    def test_cleanup_success(self):
        vm = make_vm()
        marker = b"ZTP_QUIESCED"
        vm.tn.expect.return_value = (0, re.search(marker, marker), marker)
        vm.quiesce_ztp()
        self.assertNotIn("ZTP_QUIESCED", vm.wait_write.call_args.args[0])

    def test_guest_cleanup_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "systemctl"
            stub.write_text("#!/bin/sh\nexec sleep 30\n")
            stub.chmod(0o755)
            result = subprocess.run(
                ["sh", "-c", launch.quiesce_ztp_cmd(timeout=1)],
                check=False,
                env=dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}"),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.stdout.strip(), "ZTP_FAILED")

    def test_guest_cleanup_orders_stop_delete_and_network_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls"
            for name in ("systemctl", "sonic-db-cli"):
                stub = root / name
                stub.write_text(
                    '#!/bin/sh\necho "$(basename "$0") $*" >> "$CALLS"\n'
                    '[ "$*" = "$FAIL" ] && exit 1\n'
                    'case "$*" in\n'
                    'show*LoadState*) echo "$LOAD_STATE";;\n'
                    "show*ActiveState*) echo inactive;;\n"
                    "esac\nexit 0\n"
                )
                stub.chmod(0o755)
            for load, failure, expected in (
                ("loaded", "", "ZTP_QUIESCED"),
                ("not-found", "", "ZTP_QUIESCED"),
                ("error", "", "ZTP_FAILED"),
                ("loaded", "stop ztp", "ZTP_FAILED"),
                ("loaded", "CONFIG_DB DEL ZTP|mode", "ZTP_FAILED"),
                ("loaded", "restart interfaces-config", "ZTP_FAILED"),
            ):
                with self.subTest(load=load, failure=failure):
                    log.write_text("")
                    result = subprocess.run(
                        ["sh", "-c", launch.quiesce_ztp_cmd(timeout=2)],
                        check=False,
                        env=dict(
                            os.environ,
                            PATH=f"{root}:{os.environ['PATH']}",
                            CALLS=str(log),
                            LOAD_STATE=load,
                            FAIL=failure,
                        ),
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(result.stdout.strip(), expected, result.stderr)
                    calls = log.read_text()
                    if load == "not-found":
                        self.assertNotIn("stop ztp", calls)
                        self.assertNotIn("sonic-db-cli", calls)
                    if load == "loaded" and not failure:
                        self.assertLess(
                            calls.index("stop ztp"), calls.index("CONFIG_DB DEL")
                        )
                        self.assertLess(
                            calls.index("CONFIG_DB DEL"),
                            calls.index("restart interfaces-config"),
                        )


if __name__ == "__main__":
    unittest.main()
