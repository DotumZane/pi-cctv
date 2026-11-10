import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cctv.os_update import run_update


class OsUpdateTests(unittest.TestCase):
    def make_state(self, path: Path) -> Path:
        state_path = path / "device.json"
        state_path.write_text(json.dumps({
            "id": "cam-1", "server": "https://server", "device_token": "secret-token",
        }))
        return state_path

    def test_runs_fixed_noninteractive_update_and_reports_success_without_rebooting(self):
        with TemporaryDirectory() as directory:
            state_path = self.make_state(Path(directory))
            completed = subprocess.CompletedProcess([], 0, stdout="packages upgraded", stderr="")
            with patch("cctv.os_update.report") as report, \
                    patch("cctv.os_update.subprocess.run", return_value=completed) as run, \
                    patch("cctv.os_update.Path.exists", return_value=False):
                self.assertEqual(run_update(state_path), 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0], ["apt-get", "update"])
            self.assertEqual(run.call_args_list[1].args[0][:3], ["apt-get", "-y", "upgrade"])
            self.assertEqual(run.call_args_list[0].kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")
            self.assertEqual([call.args[1] for call in report.call_args_list], ["running", "succeeded"])
            self.assertFalse(report.call_args_list[-1].args[3])

    def test_stops_after_package_failure_and_reports_failure(self):
        with TemporaryDirectory() as directory:
            state_path = self.make_state(Path(directory))
            failed = subprocess.CompletedProcess([], 100, stdout="", stderr="apt failed")
            with patch("cctv.os_update.report") as report, \
                    patch("cctv.os_update.subprocess.run", return_value=failed) as run, \
                    patch("cctv.os_update.Path.exists", return_value=False):
                self.assertEqual(run_update(state_path), 1)
            run.assert_called_once()
            self.assertEqual(report.call_args_list[-1].args[1], "failed")
            self.assertIn("apt-get exited with status 100", report.call_args_list[-1].args[2])


if __name__ == "__main__":
    unittest.main()
