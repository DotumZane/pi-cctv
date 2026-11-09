import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from cctv.server import Handler
from cctv.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.store = Store(":memory:")
        self.httpd.bootstrap_token = "bootstrap-token"
        self.recordings = tempfile.TemporaryDirectory()
        self.httpd.recordings_dir = self.recordings.name
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = ("127.0.0.1", self.httpd.server_port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.store.close()
        self.recordings.cleanup()

    def call(self, method, path, payload=None, token="", bootstrap=""):
        connection = HTTPConnection(*self.base)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if bootstrap:
            headers["X-Bootstrap-Token"] = bootstrap
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_register_and_assign(self):
        self.assertEqual(self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})[0], 201)
        self.assertEqual(self.call("POST", "/api/devices", {"id": "mon-1", "kind": "monitor", "name": "Control"})[0], 201)
        status, assignment = self.call("PUT", "/api/monitors/mon-1/assignment", {"layout": "single", "cameras": ["cam-1"]})
        self.assertEqual(status, 200)
        self.assertEqual(assignment["layout"], "single")
        self.assertEqual(json.loads(assignment["cameras"]), ["cam-1"])

    def test_rejects_unknown_camera(self):
        self.call("POST", "/api/devices", {"id": "mon-1", "kind": "monitor", "name": "Control"})
        status, _ = self.call("PUT", "/api/monitors/mon-1/assignment", {"layout": "single", "cameras": ["missing"]})
        self.assertEqual(status, 400)

    def test_two_display_monitor_requires_one_camera_per_output(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        self.call("POST", "/api/devices", {"id": "cam-2", "kind": "camera", "name": "Saw 2"})
        self.call("POST", "/api/devices", {"id": "mon-1", "kind": "monitor", "name": "Control"})
        self.call("POST", "/api/devices/mon-1/heartbeat", {"displays": [{"id": "HDMI-A-1"}, {"id": "HDMI-A-2"}]})
        status, _ = self.call("PUT", "/api/monitors/mon-1/assignment", {"layout": "2x2", "cameras": ["cam-1", "cam-2"]})
        self.assertEqual(status, 400)
        status, assignment = self.call("PUT", "/api/monitors/mon-1/assignment", {
            "layout": "single", "cameras": ["cam-1", "cam-2"],
            "outputs": [
                {"display_id": "HDMI-A-1", "layout": "single", "cameras": ["cam-1"]},
                {"display_id": "HDMI-A-2", "layout": "single", "cameras": ["cam-2"]},
            ],
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(assignment["outputs"])), 2)

    def test_monitor_settings_are_persisted(self):
        self.call("POST", "/api/devices", {"id": "mon-1", "kind": "monitor", "name": "Control"})
        status, device = self.call("PUT", "/api/monitors/mon-1/settings", {
            "scale_mode": "fill", "orientation": 0, "width": 1920, "height": 1080,
            "refresh_hz": 60, "auto_resolution": True, "kiosk": True, "prevent_blanking": True,
            "show_camera_name": True, "show_timestamp": True,
            "reconnect_seconds": 5, "watchdog_seconds": 30,
            "fallback_camera": "", "schedule_enabled": False,
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(device["metadata"])["settings"]["orientation"], 0)
        self.assertTrue(json.loads(device["metadata"])["settings"]["auto_resolution"])
        status, device = self.call("PUT", "/api/monitors/mon-1/settings", {"orientation": 90})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(device["metadata"])["settings"]["orientation"], 90)

    def test_optional_admin_token_protects_mutations(self):
        self.httpd.admin_token = "test-token"
        self.assertEqual(self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})[0], 401)
        self.assertEqual(self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"}, "test-token")[0], 201)

    def test_read_token_allows_inventory_but_not_mutations(self):
        self.httpd.admin_token = "admin-token"
        self.httpd.read_token = "read-token"
        self.assertEqual(self.call("GET", "/api/devices", token="read-token")[0], 200)
        self.assertEqual(self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"}, "read-token")[0], 401)

    def test_camera_settings_are_persisted(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        status, device = self.call("PUT", "/api/cameras/cam-1/settings", {"width": 1280, "height": 720, "fps": 30, "bitrate": 4000000})
        self.assertEqual(status, 200)
        metadata = json.loads(device["metadata"])
        self.assertEqual(metadata["settings"]["width"], 1280)

    def test_heartbeat_metrics_do_not_overwrite_camera_settings(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        self.call("PUT", "/api/cameras/cam-1/settings", {"width": 1280})
        self.call("POST", "/api/devices/cam-1/heartbeat", {"metrics": {"cpu_temperature_c": 55.2}})
        device = self.call("GET", "/api/devices/cam-1")[1]
        metadata = json.loads(device["metadata"])
        self.assertEqual(metadata["settings"]["width"], 1280)
        self.assertEqual(metadata["metrics"]["cpu_temperature_c"], 55.2)

    def test_mutations_create_audit_entries(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        status, entries = self.call("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertEqual(entries["entries"][0]["action"], "device.register")

    def test_device_delete_removes_device_and_assignment(self):
        self.call("POST", "/api/devices", {"id": "mon-1", "kind": "monitor", "name": "Control"})
        status, result = self.call("DELETE", "/api/devices/mon-1")
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], "mon-1")
        self.assertEqual(self.call("GET", "/api/devices/mon-1")[0], 404)

    def test_recording_index_and_safe_download(self):
        recording = Path(self.recordings.name) / "cam-01" / "2026-11-04" / "06-00-00.mkv"
        recording.parent.mkdir(parents=True)
        recording.write_bytes(b"test-video")
        old_time = time.time() - 20
        os.utime(recording, (old_time, old_time))
        status, index = self.call("GET", "/api/recordings?camera_id=cam-01")
        self.assertEqual(status, 200)
        self.assertEqual(index["recordings"][0]["path"], "cam-01/2026-11-04/06-00-00.mkv")

    def test_restart_media_command_is_queued_and_consumed(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        status, command = self.call("POST", "/api/devices/cam-1/commands", {"command": "restart_media"})
        self.assertEqual(status, 202)
        self.assertEqual(command["command"], "restart_media")
        status, pending = self.call("GET", "/api/devices/cam-1/commands")
        self.assertEqual(status, 200)
        self.assertEqual(pending["commands"][0]["id"], command["id"])
        self.assertEqual(self.call("GET", "/api/devices/cam-1/commands")[1]["commands"], [])

    def test_diagnostics_commands_are_allowlisted_and_history_is_available(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        status, _ = self.call("POST", "/api/devices/cam-1/commands", {"command": "shell"})
        self.assertEqual(status, 400)
        status, command = self.call("POST", "/api/devices/cam-1/commands", {"command": "diagnostics"})
        self.assertEqual(status, 202)
        status, history = self.call("GET", "/api/devices/cam-1/command-history")
        self.assertEqual(status, 200)
        self.assertEqual(history["commands"][0]["id"], command["id"])

    def test_pending_device_can_be_adopted_and_claims_device_token(self):
        status, pending = self.call("POST", "/api/enroll", {"id": "pending-pi", "name": "New Pi"}, bootstrap="bootstrap-token")
        self.assertEqual(status, 202)
        self.assertEqual(pending["status"], "pending")
        status, adopted = self.call("POST", "/api/pending/pending-pi/adopt", {"kind": "camera", "name": "Saw 1", "location": "Line 1"})
        self.assertEqual(status, 201)
        status, claim = self.call("GET", "/api/enroll/pending-pi", bootstrap="bootstrap-token")
        self.assertEqual(status, 200)
        self.assertEqual(claim["status"], "adopted")
        self.assertTrue(claim["device_token"])
        self.assertEqual(self.call("POST", "/api/devices/pending-pi/heartbeat", {"test": True}, claim["device_token"])[0], 200)

    def test_camera_live_urls_are_available(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        status, live = self.call("GET", "/api/cameras/cam-1/live")
        self.assertEqual(status, 200)
        self.assertTrue(live["hls_url"].endswith("/cam-1/index.m3u8"))


if __name__ == "__main__":
    unittest.main()
