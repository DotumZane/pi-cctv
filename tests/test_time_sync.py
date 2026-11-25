import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv import time_sync
from cctv.runtime import DeviceRuntime


class TimeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf_dir = Path(self.tmp.name) / "timesyncd.conf.d"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dashboard_becomes_the_time_server_without_removing_fallbacks(self):
        self.assertTrue(time_sync.configure("https://192.168.10.10:8443", self.conf_dir, True))
        content = (self.conf_dir / time_sync.CONF_NAME).read_text()
        self.assertIn("[Time]\nNTP=192.168.10.10 0.debian.pool.ntp.org ", content)
        self.assertNotIn("FallbackNTP", content)
        self.assertEqual((self.conf_dir / time_sync.CONF_NAME).stat().st_mode & 0o777, 0o644)

    def test_unchanged_setting_is_not_rewritten(self):
        self.assertTrue(time_sync.configure("https://cctv.lan:8443", self.conf_dir, True))
        self.assertFalse(time_sync.configure("https://cctv.lan:8443", self.conf_dir, True))
        self.assertTrue(time_sync.configure("https://cctv2.lan:8443", self.conf_dir, True))

    def test_skips_pis_without_timesyncd_and_unusable_hosts(self):
        self.assertFalse(time_sync.configure("https://cctv.lan:8443", self.conf_dir, False))
        self.assertFalse(time_sync.configure("http://127.0.0.1:8080", self.conf_dir, True))
        self.assertFalse(self.conf_dir.exists())

    def test_timesyncd_is_only_restarted_when_the_setting_changes(self):
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "https://cctv.lan:8443", "media")
        with patch.object(time_sync, "configure", return_value=False), patch.object(time_sync, "apply") as apply:
            runtime.configure_time_source()
        apply.assert_not_called()
        with patch.object(time_sync, "configure", return_value=True), patch.object(time_sync, "apply") as apply:
            runtime.configure_time_source()
        apply.assert_called_once_with()

    def test_failures_never_stop_the_agent(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://cctv.lan:8443", "media")
        with patch.object(time_sync, "configure", side_effect=PermissionError("read-only")):
            runtime.configure_time_source()

    def test_apply_uses_try_restart_with_a_timeout(self):
        with patch.object(time_sync.subprocess, "run") as run:
            time_sync.apply()
        self.assertEqual(run.call_args.args[0], ["systemctl", "try-restart", "systemd-timesyncd.service"])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
