import unittest
import datetime
import json
import shlex
import time
import os
import fnmatch
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from cctv.agent import run_pending
from cctv.runtime import DeviceRuntime, _monitor_output_size, apply_monitor_schedule, camera_stream_published, command_for_assignment, commands_for_assignment, managed_monitor_commands, start_monitor_splash_setup


class RuntimeTests(unittest.TestCase):
    def test_boot_splash_setup_runs_only_on_monitor_pi(self):
        with TemporaryDirectory(prefix="pi-cctv-splash-test-") as directory:
            root = Path(directory)
            script = root / "scripts/install-monitor-splash.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/bash\n")
            cmdline = root / "cmdline.txt"
            cmdline.write_text("console=tty1\n")
            with patch("cctv.runtime.os.geteuid", return_value=0), \
                    patch("cctv.runtime.subprocess.run", return_value=Mock(returncode=0)) as run:
                start_monitor_splash_setup("camera", root, cmdline)
                run.assert_not_called()
                start_monitor_splash_setup("monitor", root, cmdline)
                self.assertEqual(run.call_count, 1)
                args = run.call_args.args[0]
                self.assertIn("--no-block", args)
                self.assertIn(f"--setenv=CCTV_INSTALL_DIR={root}", args)
                self.assertEqual(args[-2:], ["/bin/bash", str(script)])
                cmdline.unlink()
                start_monitor_splash_setup("monitor", root, cmdline)
                self.assertEqual(run.call_count, 1)

    def test_monitor_preview_uses_current_pipeline_and_retries_failed_upload(self):
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media", token="token")
        with TemporaryDirectory(prefix="pi-cctv-test-") as directory:
            current = Path(directory) / "pi-cctv-monitor-preview-mon-1-42-00001.jpg"
            other = Path(directory) / "pi-cctv-monitor-preview-mon-2-42-00001.jpg"
            old = Path(directory) / "pi-cctv-monitor-preview-mon-1-42-00000.jpg"
            for path, contents in ((current, b"\xff\xd8current\xff\xd9"),
                                   (other, b"\xff\xd8other\xff\xd9"),
                                   (old, b"\xff\xd8old\xff\xd9")):
                path.write_bytes(contents)
            now = time.time_ns()
            for path, stamp in ((current, now - 2_000_000_000),
                                (other, now - 1_500_000_000),
                                (old, now - 8_000_000_000)):
                os.utime(path, ns=(stamp, stamp))
            runtime.last_command = ("multifilesink location=/tmp/pi-cctv-monitor-preview-mon-1-42-%05d.jpg",)
            runtime.preview_started_ns = now - 5_000_000_000
            def matching_files(pattern):
                return [str(path) for path in (current, other, old) if fnmatch.fnmatch(path.name, Path(pattern).name)]
            with patch("cctv.runtime.glob.glob", side_effect=matching_files), \
                    patch("cctv.runtime.urllib.request.urlopen", side_effect=[OSError("offline"), MagicMock()]) as upload:
                runtime.upload_monitor_preview()
                self.assertEqual(runtime.last_preview_seen_mtime_ns, 0)
                runtime.upload_monitor_preview()
            self.assertEqual(upload.call_count, 2)
            self.assertEqual(upload.call_args.args[0].data, b"\xff\xd8current\xff\xd9")
            self.assertEqual(runtime.last_preview_seen_mtime_ns, current.stat().st_mtime_ns)

    def test_camera_publisher_probe_checks_metadata_only(self):
        connection = Mock()
        connection.recv.return_value = b"RTSP/1.0 404 Not Found\r\n"
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=None)
        with patch("cctv.runtime.socket.create_connection", return_value=connection):
            self.assertFalse(camera_stream_published("media", "cam-1"))
        sent = connection.sendall.call_args.args[0]
        self.assertIn(b"DESCRIBE rtsp://media:8554/cam-1", sent)
        self.assertNotIn(b"PLAY ", sent)

    def test_camera_falls_back_when_high_resolution_never_publishes(self):
        processes = []
        def factory(*args, **kwargs):
            process = Mock()
            process.poll.return_value = None
            processes.append(process)
            return process
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "http://server", "media", process_factory=factory)
        device = {"metadata": '{"settings":{"width":2560,"height":1440}}'}
        clock = [0]
        with patch("cctv.runtime.http_json", return_value=device), \
                patch("cctv.runtime.camera_stream_published", return_value=False), \
                patch.object(runtime, "stop_media", side_effect=lambda: runtime.processes.clear()), \
                patch("cctv.runtime.time.monotonic", side_effect=lambda: clock[0]):
            runtime.reconcile_media()
            clock[0] = 21
            runtime.reconcile_media()
            clock[0] = 42
            runtime.reconcile_media()
        self.assertEqual(len(processes), 3)
        self.assertIn("--width 2560 --height 1440", runtime.camera_requested_command[0])
        self.assertIn("--width 1920 --height 1080", runtime.last_command[0])

    def test_monitor_media_is_wrapped_in_controllable_player(self):
        command = managed_monitor_commands(['gst-launch-1.0 -e videotestsrc ! textoverlay text="camera name" ! fakesink'], "monitor-01")
        self.assertEqual(len(command), 1)
        self.assertIn("cctv.monitor_player --control-file", command[0])
        self.assertIn("monitor-stats-monitor-01.json", command[0])
        self.assertIn("--pipeline", command[0])
        self.assertIn("textoverlay", command[0])
        parts = shlex.split(command[0])
        self.assertIn('textoverlay text="camera name"', parts[parts.index("--pipeline") + 1])

    def test_show_stats_writes_atomic_control_request_for_monitor_only(self):
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media")
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"CCTV_MONITOR_STATS_DIR": directory}), \
                patch("cctv.runtime.http_json", return_value={"commands": [{"command": "show_stats"}]}):
            runtime.poll_commands()
            control = Path(directory) / "monitor-stats-mon-1.json"
            request = json.loads(control.read_text())
            self.assertTrue(request["request_id"])
            self.assertEqual(control.stat().st_mode & 0o777, 0o600)

        camera = DeviceRuntime("camera", "cam-1", "Camera", "", "http://server", "media")
        with patch("cctv.runtime.http_json", return_value={"commands": [{"command": "show_stats"}]}):
            camera.poll_commands()

    def test_set_hostname_uses_hostnamectl(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "http://server", "media")
        with patch("cctv.runtime.http_json", return_value={"commands": [{"command": "set_hostname", "payload": {"hostname": "front-door"}}]}), \
                patch("cctv.runtime.subprocess.run", return_value=Mock(returncode=0, stdout="", stderr="")) as run:
            runtime.poll_commands()
        run.assert_called_once_with(["hostnamectl", "set-hostname", "front-door"], check=False, capture_output=True, text=True, timeout=15)

    def test_server_address_change_verifies_before_persisting(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://old.example", "old-media", token="device-token")
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "device.json"
            with patch.dict("os.environ", {"CCTV_AGENT_STATE_FILE": str(state_path)}), \
                    patch("cctv.runtime.http_json", return_value={"ok": True}) as request:
                ok, message = runtime.change_server_address("https://new.example:8443", "192.168.10.10")
            self.assertTrue(ok, message)
            self.assertEqual(runtime.server, "https://new.example:8443")
            self.assertEqual(runtime.media_server, "192.168.10.10")
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["server"], runtime.server)
            self.assertEqual(saved["media_server"], runtime.media_server)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("https://new.example:8443/api/devices/cam-1/heartbeat", request.call_args.args[0])

    def test_server_address_change_keeps_current_endpoint_when_verification_fails(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://old.example", "old-media", token="device-token")
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "device.json"
            with patch.dict("os.environ", {"CCTV_AGENT_STATE_FILE": str(state_path)}), \
                    patch("cctv.runtime.http_json", side_effect=OSError("certificate verify failed")):
                ok, message = runtime.change_server_address("https://new.example", "new-media")
            self.assertFalse(ok)
            self.assertIn("current server address", message)
            self.assertEqual(runtime.server, "https://old.example")
            self.assertEqual(runtime.media_server, "old-media")
            self.assertFalse(state_path.exists())

    def test_empty_assignment_stops_monitor_pipeline(self):
        self.assertIn("No camera assigned", command_for_assignment({"layout": "single", "cameras": "[]"}, "media"))

    def test_single_assignment_targets_camera_path(self):
        command = command_for_assignment({"layout": "single", "cameras": '["cam-7"]'}, "media")
        self.assertIn("rtsp://media:8554/cam-7", command)

    def test_direct_sources_work_for_single_quad_and_multiple_outputs(self):
        assignment = {"layout": "2x2", "cameras": '["a", "b"]',
                      "camera_sources": {"a": "192.168.10.21", "b": "192.168.10.22"}}
        quad = command_for_assignment(assignment, "nvr")
        self.assertIn("rtsp://192.168.10.21:8554/a", quad)
        self.assertIn("rtsp://192.168.10.22:8554/b", quad)
        assignment["outputs"] = '[{"cameras":["a"]},{"cameras":["b"]}]'
        commands = commands_for_assignment(assignment, "nvr")
        self.assertIn("rtsp://192.168.10.21:8554/a", commands[0])
        self.assertIn("rtsp://192.168.10.22:8554/b", commands[1])

    def test_monitor_uses_cached_direct_route_when_dashboard_is_unavailable(self):
        with TemporaryDirectory() as directory:
            runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "https://server", "nvr")
            runtime.runtime_cache_path = Path(directory) / "runtime-cache.json"
            device = {"metadata": '{"settings":{"kiosk":true}}'}
            assignment = {"layout": "single", "cameras": '["cam-1"]',
                          "camera_sources": {"cam-1": "192.168.10.21"},
                          "offline_cameras": ["cam-1"], "fallback_camera": "cam-backup"}
            with patch("cctv.runtime.http_json", side_effect=[device, assignment]), \
                    patch("cctv.runtime.connected_displays", return_value=[]), \
                    patch.object(runtime, "apply_monitor_blanking"):
                first = runtime.desired_command()
            with patch("cctv.runtime.http_json", side_effect=OSError("server down")), \
                    patch("cctv.runtime.connected_displays", return_value=[]), \
                    patch.object(runtime, "apply_monitor_blanking"):
                restarted = DeviceRuntime("monitor", "mon-1", "Monitor", "", "https://server", "nvr")
                restarted.runtime_cache_path = runtime.runtime_cache_path
                recovered = restarted.desired_command()
            self.assertIn("rtsp://nvr:8554/cam-backup", first[0])
            self.assertIn("rtsp://192.168.10.21:8554/cam-1", recovered[0])
            self.assertEqual(runtime.runtime_cache_path.stat().st_mode & 0o777, 0o600)

    def test_nvr_forwarder_copies_local_stream_without_new_encoder(self):
        camera = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "nvr")
        camera.processes = [Mock(poll=Mock(return_value=None))]
        camera.process_factory = Mock(return_value=Mock(poll=Mock(return_value=None)))
        camera.reconcile_forwarder()
        command = camera.process_factory.call_args.args[0]
        self.assertIn("-c:v copy", command)
        self.assertIn("rtsp://127.0.0.1:8554/cam-1", command)
        self.assertIn("rtsp://nvr:8554/cam-1", command)
        self.assertNotIn("rpicam-vid", command)
        camera.reconcile_forwarder()
        camera.process_factory.assert_called_once()

    def test_camera_keeps_local_capture_when_dashboard_is_unavailable(self):
        with TemporaryDirectory() as directory:
            camera = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "nvr")
            camera.runtime_cache_path = Path(directory) / "runtime-cache.json"
            camera._cached_config(settings={"width": 1280, "height": 720})
            with patch("cctv.runtime.http_json", side_effect=OSError("server down")):
                command = camera.desired_command()[0]
            self.assertIn("--width 1280 --height 720", command)
            self.assertIn("rtsp://127.0.0.1:8554/cam-1", command)
            self.assertNotIn("rtsp://nvr:8554", command)

    def test_camera_only_advertises_direct_route_after_stream_is_published(self):
        camera = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "nvr")
        with patch("cctv.runtime.camera_stream_published", side_effect=[False, True]), \
                patch("cctv.runtime.http_json", return_value={}) as request:
            camera.heartbeat()
            camera.heartbeat()
        self.assertFalse(request.call_args_list[0].args[2]["direct_rtsp_ready"])
        self.assertTrue(request.call_args_list[1].args[2]["direct_rtsp_ready"])

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

    def test_stalled_dashboard_preview_does_not_restart_live_monitor_pipeline(self):
        process = Mock()
        process.poll.return_value = None
        process_factory = Mock()
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media", process_factory=process_factory)
        runtime.last_command = ("pipeline",)
        runtime.processes = [process]
        runtime.next_media_retry = 999
        with patch.object(runtime, "desired_command", return_value=["pipeline"]), \
                patch.object(runtime, "stop_media") as stop_media, \
                patch("cctv.runtime.time.monotonic", return_value=10_000):
            runtime.reconcile_media()
        stop_media.assert_not_called()
        process_factory.assert_not_called()

    def test_exited_monitor_pipeline_is_restarted_after_retry_delay(self):
        process = Mock()
        process.poll.return_value = 1
        replacement = Mock()
        replacement.poll.return_value = None
        process_factory = Mock(return_value=replacement)
        runtime = DeviceRuntime("monitor", "mon-1", "Monitor", "", "http://server", "media", process_factory=process_factory)
        runtime.last_command = ("pipeline",)
        runtime.processes = [process]
        runtime.next_media_retry = 10
        with patch.object(runtime, "desired_command", return_value=["pipeline"]), \
                patch("cctv.runtime.time.monotonic", return_value=11):
            runtime.reconcile_media()
        process_factory.assert_called_once_with("pipeline", shell=True, start_new_session=True)

    def test_monitor_schedule_selects_weekday_route_and_handles_overnight(self):
        assignment = {"layout": "single", "cameras": '["cam-default"]', "outputs": "[]"}
        settings = {"schedule_enabled": True, "schedules": [
            {"day": 0, "start": "09:00", "end": "17:00", "layout": "single", "cameras": ["cam-day"]},
        ]}
        daytime = apply_monitor_schedule(assignment, settings, datetime.datetime(2026, 11, 9, 10, 0))
        self.assertEqual(json.loads(daytime["cameras"]), ["cam-day"])
        overnight_settings = {"schedule_enabled": True, "schedules": [
            {"day": 0, "start": "22:00", "end": "02:00", "layout": "single", "cameras": ["cam-night"]},
        ]}
        overnight = apply_monitor_schedule(assignment, overnight_settings, datetime.datetime(2026, 11, 10, 1, 0))
        self.assertEqual(json.loads(overnight["cameras"]), ["cam-night"])

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
        self.assertIn("rtspclientsink location=rtsp://127.0.0.1:8554/cam-7", command)
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
                patch("cctv.runtime.subprocess.run") as run, \
                patch.object(Path, "read_text", return_value="boot-id\n"), \
                patch.object(runtime, "report_maintenance") as report:
            with self.assertRaisesRegex(SystemExit, "reboot requested"):
                runtime.poll_commands()
        run.assert_called_once_with(["systemctl", "reboot"], check=True, timeout=10)
        self.assertEqual(report.call_args.args[:3], ("reboot", "running", "Reboot requested; waiting for the Pi to return."))

    def test_reboot_is_confirmed_only_after_boot_id_changes(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "media", token="device-token")
        response = {"maintenance": {"state": "running", "details": {"boot_id": "old-boot"}}}
        with patch("cctv.runtime.http_json", return_value=response) as request, \
                patch("cctv.runtime.Path.read_text", return_value="new-boot\n"), \
                patch.object(runtime, "report_maintenance") as report:
            runtime.confirm_reboot_after_boot()
        request.assert_called_once()
        self.assertEqual(report.call_args.args[:3], ("reboot", "succeeded", "Pi reboot completed and the agent is back online."))
        self.assertEqual(report.call_args.args[3], {"boot_id": "new-boot"})

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
            adopted = {"status": "adopted", "id": "cctv-cam-01", "kind": "camera", "name": "Cam 1", "location": "", "device_token": "device-secret"}
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
