import subprocess
import unittest
from unittest.mock import Mock, patch

from cctv import update


class UpdateTests(unittest.TestCase):
    def test_update_checks_service_health_before_reporting_success(self):
        old, new = "a" * 40, "b" * 40
        with patch.object(update, "mark_setup_ap_checked_this_boot") as mark_checked, \
                patch.object(update, "run", return_value=Mock(returncode=0, stdout="", stderr="")) as run, \
                patch.object(update, "current_commit", side_effect=[old, new, new]), \
                patch.object(update, "wait_for_healthy_service", return_value=True) as healthy, \
                patch.object(update, "report_status") as report:
            self.assertEqual(update.main(), 0)
        self.assertEqual(run.call_count, 4)
        mark_checked.assert_called_once_with()
        healthy.assert_called_once_with()
        self.assertEqual(report.call_args_list[0].args[0], "running")
        self.assertEqual(report.call_args_list[-1].args[0], "succeeded")

    def test_unhealthy_update_rolls_back_and_reports_failure(self):
        old, new = "a" * 40, "b" * 40
        with patch.object(update, "mark_setup_ap_checked_this_boot"), \
                patch.object(update, "run", return_value=Mock(returncode=0, stdout="", stderr="")), \
                patch.object(update, "current_commit", side_effect=[old, new, new]), \
                patch.object(update, "wait_for_healthy_service", return_value=False), \
                patch.object(update, "rollback", return_value=True) as rollback, \
                patch.object(update, "report_status") as report:
            self.assertEqual(update.main(), 1)
        rollback.assert_called_once_with(old)
        self.assertEqual(report.call_args_list[-1].args[0], "failed")
        self.assertIn("Automatic rollback restored the previous version", report.call_args_list[-1].args[1])

    def test_failed_pull_does_not_attempt_rollback(self):
        old = "a" * 40
        failed = Mock(returncode=7, stdout="", stderr="network unavailable")
        with patch.object(update, "run", side_effect=[Mock(returncode=0, stdout="", stderr=""), failed]), \
                patch.object(update, "current_commit", return_value=old), \
                patch.object(update, "rollback") as rollback, \
                patch.object(update, "report_status") as report:
            self.assertEqual(update.main(), 1)
        rollback.assert_not_called()
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
