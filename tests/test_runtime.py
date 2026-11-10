import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from cctv.agent import run_pending
from cctv.runtime import DeviceRuntime, _monitor_output_size, command_for_assignment, commands_for_assignment


class RuntimeTests(unittest.TestCase):
    def test_empty_assignment_stops_monitor_pipeline(self):
        self.assertIsNone(command_for_assignment({"layout": "single", "cameras": "[]"}, "media"))

    def test_single_assignment_targets_camera_path(self):
        command = command_for_assignment({"layout": "single", "cameras": '["cam-7"]'}, "media")
        self.assertIn("rtsp://media:8554/cam-7", command)

    def test_monitor_auto_resolution_uses_detected_native_mode(self):
        settings = {"auto_resolution": True, "display_modes": {"32": {"width": "800", "height": "480"}}}
        self.assertEqual(_monitor_output_size(settings, "32"), (800, 480))
        command = command_for_assignment({"layout": "single", "cameras": '["cam-7"]'}, "media", settings)
        self.assertIn("video/x-raw,width=800,height=480", command)

    def test_monitor_manual_resolution_remains_available(self):
        self.assertEqual(_monitor_output_size({"auto_resolution": False, "width": 1280, "height": 720}), (1280, 720))

    def test_monitor_applies_saved_reconnect_and_blanking_settings(self):
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media")
        device = {"metadata": '{"settings":{"reconnect_seconds":17,"prevent_blanking":false}}'}
        assignment = {"layout": "single", "cameras": '["cam-1"]'}
        with patch("cctv.runtime.http_json", side_effect=[device, assignment]), \
                patch("cctv.runtime.connected_displays", return_value=[]), \
                patch.object(runtime, "apply_monitor_blanking") as apply_blanking:
            runtime.desired_command()
        self.assertEqual(runtime.media_retry_seconds, 17)
        apply_blanking.assert_called_once_with(False)

    def test_monitor_routes_offline_camera_to_available_fallback(self):
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media")
        device = {"metadata": '{"settings":{"fallback_camera":"cam-backup"}}'}
        assignment = {
            "layout": "single", "cameras": '["cam-primary"]', "outputs": "[]",
            "offline_cameras": ["cam-primary"], "fallback_camera": "cam-backup",
        }
        with patch("cctv.runtime.http_json", side_effect=[device, assignment]), \
                patch("cctv.runtime.connected_displays", return_value=[]), \
                patch.object(runtime, "apply_monitor_blanking"):
            command = runtime.desired_command()[0]
        self.assertIn("rtsp://media:8554/cam-backup", command)
        self.assertNotIn("rtsp://media:8554/cam-primary", command)

    def test_monitor_retries_failed_pipeline_after_configured_delay(self):
        process = Mock()
        process.poll.return_value = 1
        process_factory = Mock(return_value=process)
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media", process_factory=process_factory)
        runtime.media_retry_seconds = 10
        runtime.last_command = ("pipeline",)
        runtime.processes = [process]
        with patch.object(runtime, "desired_command", return_value=["pipeline"]), \
                patch("cctv.runtime.time.monotonic", return_value=100):
            runtime.next_media_retry = 105
            runtime.reconcile_media()
        process_factory.assert_not_called()

    def test_monitor_watchdog_restarts_live_pipeline_without_preview_progress(self):
        process = Mock()
        process.poll.return_value = None
        new_process = Mock()
        new_process.poll.return_value = None
        process_factory = Mock(return_value=new_process)
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media", process_factory=process_factory)
        runtime.last_command = ("pipeline",)
        runtime.processes = [process]
        runtime.last_preview_progress_at = 1
        runtime.monitor_watchdog_seconds = 5
        runtime.next_media_retry = 999
        with patch.object(runtime, "desired_command", return_value=["pipeline"]), \
                patch.object(runtime, "stop_media") as stop_media, \
                patch("cctv.runtime.time.monotonic", return_value=10):
            runtime.reconcile_media()
        stop_media.assert_called_once_with()
        process_factory.assert_called_once()

    def test_monitor_activates_kiosk_virtual_terminal(self):
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media")
        with patch("cctv.runtime.os.open", return_value=15) as open_tty, \
                patch("cctv.runtime.fcntl.ioctl") as ioctl, \
                patch("cctv.runtime.os.close") as close_tty:
            runtime.activate_monitor_display()
        open_tty.assert_called_once_with("/dev/tty0", unittest.mock.ANY)
        self.assertEqual([call.args[1:] for call in ioctl.call_args_list], [(0x5606, 7), (0x5607, 7)])
        close_tty.assert_called_once_with(15)

    def test_camera_runtime_starts_pipeline_as_one_command(self):
        process = Mock()
        process.poll.return_value = None
        process_factory = Mock(return_value=process)
        runtime = DeviceRuntime("camera", "cam-7", "Camera", "", "http://server", "media", process_factory=process_factory)
        with patch("cctv.runtime.http_json", return_value={"metadata": "{}"}):
            runtime.reconcile_media()
        process_factory.assert_called_once()
        command = process_factory.call_args.args[0]
        self.assertIsInstance(command, str)
        self.assertTrue(command.startswith("rpicam-vid -n -t 0 --inline --codec h264"))
        self.assertIn("rtspclientsink location=rtsp://media:8554/cam-7", command)
        self.assertEqual(process_factory.call_args.kwargs, {"shell": True, "start_new_session": True})

    def test_camera_runtime_applies_saved_encoder_controls(self):
        runtime = DeviceRuntime("camera", "cam-7", "Camera", "", "http://server", "media")
        metadata = '{"settings":{"sharpness":0.5,"exposure_mode":"manual","shutter_us":8000,"gain":2.5,"exposure_profile":"sport","white_balance":"daylight","keyframe_interval":45,"live_profile":"high"}}'
        with patch("cctv.runtime.http_json", return_value={"metadata": metadata}):
            command = runtime.desired_command()[0]
        for option in (
            "--sharpness 0.5", "--shutter 8000", "--gain 2.5", "--exposure sport",
            "--awb daylight", "--intra 45", "--profile high",
        ):
            self.assertIn(option, command)

    def test_quad_assignment_builds_four_stream_layout(self):
        command = command_for_assignment({"layout": "2x2", "cameras": '["a", "b", "c", "d"]'}, "media")
        self.assertIn("rtsp://media:8554/a", command)
        self.assertIn("sink_3::xpos=960 sink_3::ypos=540", command)

    def test_two_display_assignment_builds_one_pipeline_per_output(self):
        commands = commands_for_assignment({"outputs": '[{"cameras":["a"],"connector_id":"32"},{"cameras":["b"],"connector_id":"33"}]'}, "media")
        self.assertEqual(len(commands), 2)
        self.assertIn("rtsp://media:8554/a", commands[0])
        self.assertIn("connector-id=32", commands[0])
        self.assertIn("rtsp://media:8554/b", commands[1])

    def test_reboot_uses_systemd_and_exits_agent_cleanly(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "http://server", "media")
        with patch("cctv.runtime.http_json", return_value={"commands": [{"command": "reboot"}]}), \
                patch("cctv.runtime.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "reboot requested"):
                runtime.poll_commands()
        run.assert_called_once_with(["systemctl", "reboot"], check=True, timeout=10)

    def test_dashboard_can_request_setup_ap_sync(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "media")
        with patch("cctv.runtime.http_json", return_value={"commands": [{"command": "sync_setup_ap"}]}), \
                patch.object(runtime, "sync_setup_ap", return_value=(True, "Applied")) as sync, \
                patch.object(runtime, "report_maintenance") as report:
            runtime.poll_commands()
        sync.assert_called_once_with()
        self.assertEqual([call.args[:2] for call in report.call_args_list], [
            ("setup-ap-sync", "running"), ("setup-ap-sync", "succeeded"),
        ])

    def test_pi_os_update_runs_in_detached_systemd_unit(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "media")
        with patch("cctv.runtime.http_json", return_value={"commands": [{"command": "update_os"}]}), \
                patch("cctv.runtime.subprocess.run", return_value=Mock(returncode=0, stdout="", stderr="")) as run:
            runtime.poll_commands()
        command = run.call_args.args[0]
        self.assertIn("--no-block", command)
        self.assertIn("--unit=pi-cctv-os-update-cam-1", command)
        self.assertEqual(command[-2:], ["-m", "cctv.os_update"])

    def test_adopted_enrollment_token_persists_and_is_reused_after_restart(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "device.json"
            adopted = {"status": "adopted", "kind": "camera", "name": "Cam 1", "location": "", "device_token": "device-secret"}
            with patch("cctv.agent.socket.gethostname", return_value="cctv-cam-01"), \
                    patch("cctv.agent.request", side_effect=[{"status": "pending"}, adopted]), \
                    patch("cctv.agent.DeviceRuntime") as runtime_class:
                run_pending("http://server", "bootstrap", "media", state_path=state_path)
            self.assertEqual(runtime_class.call_args.args[:6], ("camera", "cctv-cam-01", "Cam 1", "", "http://server", "media"))
            self.assertEqual(runtime_class.call_args.kwargs["token"], "device-secret")
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            with patch("cctv.agent.request") as request_mock, patch("cctv.agent.DeviceRuntime") as runtime_class:
                run_pending("http://server", "bootstrap", "media", state_path=state_path)
            request_mock.assert_not_called()
            runtime_class.assert_called_once_with("camera", "cctv-cam-01", "Cam 1", "", "http://server", "media", token="device-secret")

    def test_adoption_can_reissue_token_for_existing_device(self):
        from cctv.store import Store
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "devices.db")
            try:
                store.register({"id": "cam-1", "kind": "camera", "name": "Original"})
                store.db.execute("INSERT INTO pending_devices(id,name,ip_address,metadata,first_seen,last_seen) VALUES(?,?,?,?,?,?)", ("cam-1", "cam-1", "10.0.0.2", "{}", "now", "now"))
                store.db.commit()
                token_hash_before = store.device("cam-1")["token_hash"]
                result = store.adopt_pending("cam-1", "camera", "Recovered", "Line 1")
                self.assertIsNotNone(result)
                device, token = result
                self.assertEqual(device["name"], "Recovered")
                self.assertEqual(device["location"], "Line 1")
                self.assertNotEqual(device["token_hash"], token_hash_before)
                self.assertTrue(store.device_token_valid("cam-1", token))
            finally:
                store.close()
