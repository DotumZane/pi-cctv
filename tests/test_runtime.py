import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from cctv.agent import run_pending
from cctv.runtime import DeviceRuntime, command_for_assignment, commands_for_assignment


class RuntimeTests(unittest.TestCase):
    def test_empty_assignment_stops_monitor_pipeline(self):
        self.assertIsNone(command_for_assignment({"layout": "single", "cameras": "[]"}, "media"))

    def test_single_assignment_targets_camera_path(self):
        command = command_for_assignment({"layout": "single", "cameras": '["cam-7"]'}, "media")
        self.assertIn("rtsp://media:8554/cam-7", command)

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
        self.assertTrue(command.startswith("gst-launch-1.0 -e libcamerasrc"))
        self.assertIn("rtspclientsink location=rtsp://media:8554/cam-7", command)
        self.assertEqual(process_factory.call_args.kwargs, {"shell": True})

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
