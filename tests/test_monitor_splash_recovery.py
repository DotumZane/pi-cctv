import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cctv.monitor_player import MonitorPlayer


class SplashRecoveryTests(unittest.TestCase):
    def test_broken_rtsp_link_rebuilds_player_once(self):
        player = MonitorPlayer.__new__(MonitorPlayer)
        player.Gst = Mock()
        player.GLib = Mock()
        player.loop = Mock()
        player.reconnect_seconds = 5
        player.rebuild_scheduled = False
        source = Mock()
        player.cameras = {0: {"source": source, "restarting": True}}
        message = Mock()
        message.type = player.Gst.MessageType.ERROR
        message.src = source
        message.parse_error.return_value = ("stream failed", "streaming stopped, reason not-linked (-1)")
        player._on_bus_message(None, message)
        player._on_bus_message(None, message)
        player.GLib.timeout_add.assert_called_once_with(5000, player._rebuild_player)
        self.assertFalse(player._rebuild_player())
        player.loop.quit.assert_called_once()

    def test_stale_queued_frame_does_not_restore_live_display(self):
        player = MonitorPlayer.__new__(MonitorPlayer)
        player.lock = threading.Lock()
        selector = Mock()
        player.cameras = {0: {"last_frame": 10.0, "selector": selector, "showing_live": False, "restarting": False, "heading": "Camera"}}
        with patch("cctv.monitor_player.time.monotonic", return_value=20.0):
            player._show_live(0)
        selector.set_property.assert_not_called()
        self.assertFalse(player.cameras[0]["showing_live"])

    def test_source_retries_quickly_then_backs_off(self):
        player = MonitorPlayer.__new__(MonitorPlayer)
        player.Gst = Mock()
        player.GLib = Mock()
        player.reconnect_seconds = 5
        player._show_offline = Mock()
        source = Mock()
        player.cameras = {0: {"source": source, "heading": "Camera", "retry_count": 0}}
        for _ in range(7):
            player._restart_source(0)
        delays = [call.args[0] for call in player.GLib.timeout_add.call_args_list]
        self.assertEqual(delays, [250, 500, 1000, 2000, 4000, 5000, 5000])
        self.assertEqual(source.set_state.call_count, 7)

    def test_stable_video_resets_retry_backoff(self):
        player = MonitorPlayer.__new__(MonitorPlayer)
        player.lock = threading.Lock()
        player._show_offline = Mock()
        player._restart_source = Mock()
        player.cameras = {0: {"last_frame": 20.0, "healthy_since": 9.0, "retry_count": 4, "last_retry_at": 0, "restarting": False}}
        with patch("cctv.monitor_player.time.monotonic", return_value=21.0):
            player._check_cameras()
        self.assertEqual(player.cameras[0]["retry_count"], 0)
        player._restart_source.assert_not_called()

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
