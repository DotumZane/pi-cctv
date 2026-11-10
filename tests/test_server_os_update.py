import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "pi-cctv-server-os-update.py"
SPEC = importlib.util.spec_from_file_location("pi_cctv_server_os_update", MODULE_PATH)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class ServerOsUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.request_id = "a" * 32
        (self.state / "os-request.json").write_text(json.dumps({"id": self.request_id, "action": "os_update"}))
        os_release = self.root / "os-release"
        os_release.write_text("NAME=Ubuntu\n")
        self.constants = patch.multiple(
            updater,
            STATE_DIR=self.state,
            REQUEST=self.state / "os-request.json",
            STATUS=self.state / "os-status.json",
            LOCK=self.root / "lock",
            OS_RELEASE=os_release,
        )
        self.constants.start()

    def tearDown(self):
        self.constants.stop()
        self.tmp.cleanup()

    def test_runs_fixed_noninteractive_package_upgrade_without_rebooting(self):
        with patch.object(updater, "run") as run:
            self.assertEqual(updater.main(), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [["apt-get", "update"], ["apt-get", "-y", "upgrade"]])
        self.assertFalse((self.state / "os-request.json").exists())
        status = json.loads((self.state / "os-status.json").read_text())
        self.assertEqual(status["state"], "succeeded")
        self.assertFalse(status["reboot_required"])

    def test_stops_and_reports_failure_when_package_command_fails(self):
        with patch.object(updater, "run", side_effect=RuntimeError("apt-get failed")) as run:
            self.assertEqual(updater.main(), 1)
        self.assertEqual(run.call_count, 1)
        status = json.loads((self.state / "os-status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIn("apt-get failed", status["error"])


if __name__ == "__main__":
    unittest.main()
