import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "pi-cctv-server-reboot.py"
SPEC = importlib.util.spec_from_file_location("pi_cctv_server_reboot", MODULE_PATH)
rebooter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rebooter)


class ServerRebootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.request_id = "a" * 32
        (self.state / "reboot-request.json").write_text(json.dumps({"id": self.request_id, "action": "reboot"}))
        boot_id = self.root / "boot_id"
        boot_id.write_text("current-boot\n")
        self.constants = patch.multiple(
            rebooter,
            STATE_DIR=self.state,
            REQUEST=self.state / "reboot-request.json",
            STATUS=self.state / "reboot-status.json",
            LOCK=self.root / "lock",
            BOOT_ID=boot_id,
        )
        self.constants.start()

    def tearDown(self):
        self.constants.stop()
        self.tmp.cleanup()

    def test_queues_systemd_reboot_and_persists_rebooting_status(self):
        with patch.object(rebooter.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            self.assertEqual(rebooter.main(), 0)
        run.assert_called_once_with(["systemctl", "reboot"], text=True, capture_output=True, timeout=60, check=False)
        self.assertFalse((self.state / "reboot-request.json").exists())
        status = json.loads((self.state / "reboot-status.json").read_text())
        self.assertEqual(status["state"], "rebooting")
        self.assertEqual(status["boot_id"], "current-boot")

    def test_reports_systemctl_failure(self):
        with patch.object(rebooter.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="reboot denied")):
            self.assertEqual(rebooter.main(), 1)
        status = json.loads((self.state / "reboot-status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIn("reboot denied", status["error"])

    def test_boot_completion_marks_reboot_succeeded_only_after_boot_id_changes(self):
        status_file = self.state / "reboot-status.json"
        status_file.write_text(json.dumps({"state": "rebooting", "boot_id": "previous-boot"}))
        self.assertEqual(rebooter.complete_previous_reboot(), 0)
        status = json.loads(status_file.read_text())
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["message"], "Server reboot completed")

        status_file.write_text(json.dumps({"state": "rebooting", "boot_id": "current-boot"}))
        self.assertEqual(rebooter.complete_previous_reboot(), 0)
        status = json.loads(status_file.read_text())
        self.assertEqual(status["state"], "rebooting")


if __name__ == "__main__":
    unittest.main()
