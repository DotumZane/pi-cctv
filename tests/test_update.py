import subprocess
import unittest
from unittest.mock import Mock, patch

from cctv import update


class UpdateTests(unittest.TestCase):
    def test_update_reports_success_after_repository_and_service_steps(self):
        with patch.object(update, "mark_setup_ap_checked_this_boot") as mark_checked, \
                patch.object(update.subprocess, "run", return_value=Mock(returncode=0)) as run, \
                patch.object(update, "report_status") as report:
            self.assertEqual(update.main(), 0)
        self.assertEqual(run.call_count, 3)
        mark_checked.assert_called_once_with()
        self.assertEqual(report.call_args_list[0].args[0], "running")
        self.assertEqual(report.call_args_list[-1].args[0], "succeeded")

    def test_update_reports_failure_and_stops_after_failed_step(self):
        with patch.object(update, "mark_setup_ap_checked_this_boot"), \
                patch.object(update.subprocess, "run", return_value=Mock(returncode=7)) as run, \
                patch.object(update, "report_status") as report:
            self.assertEqual(update.main(), 7)
        run.assert_called_once()
        self.assertEqual(report.call_args_list[-1].args[0], "failed")

    def test_status_report_uses_the_saved_device_identity_and_token(self):
        with patch.object(update, "DEVICE_STATE") as path, patch("cctv.update.urllib.request.urlopen") as urlopen:
            path.read_text.return_value = '{"id":"cam-1","server":"https://cctv.example:8443","device_token":"secret"}'
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            urlopen.return_value = response
            update.report_status("running", "Pulling")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://cctv.example:8443/api/devices/cam-1/maintenance/agent-update")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")


if __name__ == "__main__":
    unittest.main()
