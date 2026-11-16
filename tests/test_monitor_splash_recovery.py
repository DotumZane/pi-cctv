import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cctv.monitor_player import MonitorPlayer


class SplashRecoveryTests(unittest.TestCase):
    def test_stalled_output_restarts_without_splash(self):
        with tempfile.TemporaryDirectory() as directory:
            player = MonitorPlayer.__new__(MonitorPlayer)
            player.splash_switched_at = 10.0
            player.last_video_buffer_at = 9.0
            player.lock = threading.Lock()
            player.loop = Mock()
            player.splash_fallback_file = Path(directory) / "fallback"
            with patch("cctv.monitor_player.time.monotonic", return_value=21.0):
                self.assertFalse(player._check_splash_output())
            self.assertTrue(player.splash_fallback_file.exists())
            player.loop.quit.assert_called_once()

    def test_recent_output_does_not_restart(self):
        player = MonitorPlayer.__new__(MonitorPlayer)
        player.splash_switched_at = 10.0
        player.last_video_buffer_at = 20.0
        player.lock = threading.Lock()
        player.loop = Mock()
        with patch("cctv.monitor_player.time.monotonic", return_value=21.0):
            self.assertFalse(player._check_splash_output())
        player.loop.quit.assert_not_called()
