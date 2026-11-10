import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cctv.server import Handler
from cctv.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.store = Store(":memory:")
        self.httpd.bootstrap_token = "bootstrap-token"
        self.recordings = tempfile.TemporaryDirectory()
        self.host_updates = tempfile.TemporaryDirectory()
        self.httpd.host_update_dir = self.host_updates.name
        self.httpd.recordings_dir = self.recordings.name
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = ("127.0.0.1", self.httpd.server_port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.store.close()
        self.recordings.cleanup()
        self.host_updates.cleanup()

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

    def test_admin_can_update_device_name_and_location_without_overwriting_metadata(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        self.call("POST", "/api/devices", {
            "id": "cam-1", "kind": "camera", "name": "Camera 1", "metadata": json.dumps({"agent_version": "0.1.9"})
        }, token)
        status, _ = self.call("PUT", "/api/devices/cam-1/profile", {"name": "Saw Entrance", "location": "North line"})
        self.assertEqual(status, 401)
        status, device = self.call("PUT", "/api/devices/cam-1/profile", {"name": "Saw Entrance", "location": "North line"}, token)
        self.assertEqual(status, 200)
        self.assertEqual(device["name"], "Saw Entrance")
        self.assertEqual(device["location"], "North line")
        self.assertEqual(device["kind"], "camera")
        self.assertEqual(json.loads(device["metadata"])["agent_version"], "0.1.9")
        self.assertEqual(self.httpd.store.audit_entries(1)[0]["action"], "device.profile.update")
        status, _ = self.call("PUT", "/api/devices/cam-1/profile", {"name": "  ", "location": "North line"}, token)
        self.assertEqual(status, 400)
        status, _ = self.call("PUT", "/api/devices/missing/profile", {"name": "Missing", "location": ""}, token)
        self.assertEqual(status, 404)

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
        self.httpd.store.set_assignment("mon-1", "single", ["cam-1"])
        status, result = self.call("DELETE", "/api/devices/mon-1")
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], "mon-1")
        self.assertEqual(self.call("GET", "/api/devices/mon-1")[0], 404)
        self.assertEqual(self.httpd.store.all_assignments(), [])

    def test_deleting_camera_clears_it_from_monitor_routes(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Camera"})
        self.call("POST", "/api/devices", {"id": "mon-1", "kind": "monitor", "name": "Monitor"})
        self.httpd.store.set_assignment("mon-1", "single", ["cam-1"], [{"cameras": ["cam-1"]}])

        status, result = self.call("DELETE", "/api/devices/cam-1")

        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], "cam-1")
        assignment = self.httpd.store.assignment("mon-1")
        self.assertEqual(json.loads(assignment["cameras"]), [])
        self.assertEqual(json.loads(assignment["outputs"]), [{"cameras": []}])

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

    def test_pending_enrollment_requires_https(self):
        status, _ = self.call("POST", "/api/enroll", {"id": "pending-pi", "name": "New Pi"}, bootstrap="bootstrap-token")
        self.assertEqual(status, 426)
        status, _ = self.call("GET", "/api/enroll/pending-pi", bootstrap="bootstrap-token")
        self.assertEqual(status, 426)

    def test_pending_device_can_be_adopted_and_claims_device_token(self):
        self.httpd.store.pending_register("pending-pi", "New Pi", "127.0.0.1", {})
        status, adopted = self.call("POST", "/api/pending/pending-pi/adopt", {"kind": "camera", "name": "Saw 1", "location": "Line 1"})
        self.assertEqual(status, 201)
        claim = self.httpd.store.claim_adoption("pending-pi")
        self.assertTrue(claim["issued_token"])
        self.assertEqual(self.call("POST", "/api/devices/pending-pi/heartbeat", {"test": True}, claim["issued_token"])[0], 200)

    def test_server_wifi_settings_never_return_the_password_and_require_https_to_change(self):
        self.httpd.store.create_user("admin", "correct-horse", "admin")
        self.httpd.admin_token = "test-token"
        profile = {"enabled": True, "ssid": "PiCCTV-Setup", "password": "secret-pass", "country": "US", "channel": 6, "revision": "1"}
        self.httpd.store.set_system_setting("setup_ap_profile", profile)
        status, settings = self.call("GET", "/api/server/setup-ap", token="test-token")
        self.assertEqual(status, 200)
        self.assertTrue(settings["password_configured"])
        self.assertNotIn("password", settings)
        status, result = self.call("PUT", "/api/server/setup-ap", {**profile, "password": "new-password"}, token="test-token")
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", result["error"])
        self.assertEqual(self.httpd.store.system_setting("setup_ap_profile")["password"], "secret-pass")
        self.httpd.tls_enabled = True
        with patch("cctv.server.ssl.SSLSocket", socket.socket):
            status, saved = self.call("PUT", "/api/server/setup-ap", {
                "enabled": True, "ssid": "PiCCTV-Field", "password": "new-password",
                "country": "US", "channel": 11,
            }, token="test-token")
        self.assertEqual(status, 200)
        self.assertNotIn("password", saved)
        self.assertEqual(saved["revision"], "2")
        self.assertEqual(self.httpd.store.system_setting("setup_ap_profile")["password"], "new-password")

    def test_server_update_requires_https_and_queues_only_when_helper_is_installed(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        status, body = self.call("POST", "/api/server/update", {}, token)
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", body["error"])

        state_dir = Path(self.host_updates.name)
        (state_dir / "installed").touch()
        (state_dir / "status.json").write_text('{"state":"idle"}\n')
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("POST", "/api/server/update", {}, token)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "queued")
        request = json.loads((state_dir / "request.json").read_text())
        self.assertEqual(request, {"id": body["request_id"], "action": "update"})
        self.assertEqual(self.httpd.store.audit_entries(1)[0]["action"], "server.update.request")

        (state_dir / "status.json").write_text('{"state":"running"}\n')
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("POST", "/api/server/update", {}, token)
        self.assertEqual(status, 409)
        self.assertEqual(body["state"], "running")

    def test_user_can_change_own_password_over_https(self):
        self.httpd.store.create_user("admin", "old-password", "admin")
        token = self.httpd.store.create_session("admin")
        status, body = self.call("PUT", "/api/auth/password", {
            "current_password": "old-password", "new_password": "updated-password",
        }, token)
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", body["error"])
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("PUT", "/api/auth/password", {
                "current_password": "wrong-password", "new_password": "updated-password",
            }, token)
            self.assertEqual(status, 401)
            status, body = self.call("PUT", "/api/auth/password", {
                "current_password": "old-password", "new_password": "updated-password",
            }, token)
        self.assertEqual(status, 200)
        self.assertEqual(body["username"], "admin")
        self.assertTrue(self.httpd.store.authenticate_user("admin", "updated-password"))
        self.assertIsNone(self.httpd.store.authenticate_user("admin", "old-password"))
        self.assertEqual(self.httpd.store.session_user(token)["username"], "admin")

    def test_admin_can_reset_another_users_password_and_revoke_sessions(self):
        self.httpd.store.create_user("admin", "admin-password", "admin")
        self.httpd.store.create_user("operator", "operator-password", "operator")
        admin_token = self.httpd.store.create_session("admin")
        operator_token = self.httpd.store.create_session("operator")
        path = "/api/users/operator/password"
        status, body = self.call("PUT", path, {"new_password": "temporary-password"}, admin_token)
        self.assertEqual(status, 426)
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("PUT", path, {"new_password": "temporary-password"}, admin_token)
            self.assertEqual(status, 200)
            status, body = self.call("PUT", "/api/users/admin/password", {"new_password": "bad-self-reset"}, admin_token)
        self.assertEqual(status, 400)
        self.assertIn("Change my password", body["error"])
        self.assertTrue(self.httpd.store.authenticate_user("operator", "temporary-password"))
        self.assertIsNone(self.httpd.store.session_user(operator_token))
        self.assertEqual(body["error"], "use Change my password to update your own account")
        self.assertEqual(self.httpd.store.audit_entries(1)[0]["action"], "user.password_reset")

    def test_server_update_reports_missing_host_helper(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        status, body = self.call("GET", "/api/server/update", token=token)
        self.assertEqual(status, 200)
        self.assertFalse(body["installed"])
        self.assertEqual(body["state"], "not_installed")

    def test_server_health_requires_read_auth_and_reports_runtime_and_storage(self):
        self.httpd.read_token = "read-token"
        status, _ = self.call("GET", "/api/server/health")
        self.assertEqual(status, 401)
        status, body = self.call("GET", "/api/server/health", token="read-token")
        self.assertEqual(status, 200)
        self.assertIn("version", body)
        self.assertGreaterEqual(body["process_uptime_seconds"], 0)
        self.assertIn("memory_total_bytes", body)
        self.assertIsNotNone(body["recordings_storage"])
        self.assertGreater(body["recordings_storage"]["total_bytes"], 0)
        self.assertGreaterEqual(body["database_bytes"], 0)

    def test_server_stream_addresses_are_admin_managed_and_used_for_live_urls(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        self.httpd.public_media_url = "http://media.example:8888"
        self.httpd.public_webrtc_url = "http://media.example:8889"
        status, settings = self.call("GET", "/api/server/network", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(settings["media_url"], "http://media.example:8888")

        payload = {"media_url": "https://streams.example/hls", "webrtc_url": "https://streams.example/webrtc"}
        status, body = self.call("PUT", "/api/server/network", payload, token)
        self.assertEqual(status, 426)
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("PUT", "/api/server/network", payload, token)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)

        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Camera"}, token)
        status, live = self.call("GET", "/api/cameras/cam-1/live", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(live["hls_url"], "https://streams.example/hls/cam-1/index.m3u8")
        self.assertEqual(live["webrtc_url"], "https://streams.example/webrtc/cam-1/whep")

        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("PUT", "/api/server/network", {"media_url": "javascript:alert(1)", "webrtc_url": payload["webrtc_url"]}, token)
        self.assertEqual(status, 400)

    def test_server_os_update_requires_https_and_queues_fixed_action(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        state_dir = Path(self.host_updates.name)
        (state_dir / "os-installed").touch()
        (state_dir / "os-status.json").write_text('{"state":"idle"}\n')
        status, body = self.call("POST", "/api/server/os-update", {}, token)
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", body["error"])

        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("POST", "/api/server/os-update", {}, token)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "queued")
        request = json.loads((state_dir / "os-request.json").read_text())
        self.assertEqual(request, {"id": body["request_id"], "action": "os_update"})
        self.assertEqual(self.httpd.store.audit_entries(1)[0]["action"], "server.os_update.request")

        (state_dir / "installed").touch()
        (state_dir / "os-status.json").write_text('{"state":"running"}\n')
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("POST", "/api/server/update", {}, token)
        self.assertEqual(status, 409)
        self.assertEqual(body["state"], "running")

    def test_server_os_update_reports_missing_helper(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        status, body = self.call("GET", "/api/server/os-update", token=token)
        self.assertEqual(status, 200)
        self.assertFalse(body["installed"])
        self.assertEqual(body["state"], "not_installed")

    def test_server_reboot_requires_https_and_queues_fixed_request(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        state_dir = Path(self.host_updates.name)
        (state_dir / "reboot-installed").touch()
        (state_dir / "reboot-status.json").write_text('{"state":"idle"}\n')
        status, body = self.call("POST", "/api/server/reboot", {}, token)
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", body["error"])

        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("POST", "/api/server/reboot", {}, token)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "queued")
        request = json.loads((state_dir / "reboot-request.json").read_text())
        self.assertEqual(request, {"id": body["request_id"], "action": "reboot"})
        self.assertEqual(self.httpd.store.audit_entries(1)[0]["action"], "server.reboot.request")

        (state_dir / "reboot-status.json").write_text('{"state":"rebooting"}\n')
        (state_dir / "os-installed").touch()
        with patch.object(Handler, "secure_connection", return_value=True):
            status, body = self.call("POST", "/api/server/os-update", {}, token)
        self.assertEqual(status, 409)
        self.assertEqual(body["state"], "rebooting")

    def test_server_reboot_reports_missing_helper(self):
        self.httpd.store.create_user("admin", "correct-horse-battery")
        token = self.httpd.store.create_session("admin")
        status, body = self.call("GET", "/api/server/reboot", token=token)
        self.assertEqual(status, 200)
        self.assertFalse(body["installed"])
        self.assertEqual(body["state"], "not_installed")

    def test_setup_ap_sync_command_is_allowlisted(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        device_token = "device-setup-token"
        self.httpd.store.db.execute(
            "UPDATE devices SET token_hash=? WHERE id=?",
            (self.httpd.store.hash_token(device_token), "cam-1"),
        )
        self.httpd.store.db.commit()
        self.httpd.store.create_user("admin", "correct-horse", "admin")
        admin_token = self.httpd.store.create_session("admin")
        status, command = self.call("POST", "/api/devices/cam-1/commands", {"command": "sync_setup_ap"}, admin_token)
        self.assertEqual(status, 202)
        self.assertEqual(command["command"], "sync_setup_ap")
        self.assertEqual(self.httpd.store.maintenance_run("cam-1", "setup_ap_sync")["state"], "queued")
        self.assertEqual(self.call("POST", "/api/devices/cam-1/maintenance/setup-ap-sync", {
            "state": "running", "details": {"message": "Checking profile"},
        })[0], 401)
        status, _ = self.call("POST", "/api/devices/cam-1/maintenance/setup-ap-sync", {
            "state": "running", "details": {"message": "Checking profile"},
        }, device_token)
        self.assertEqual(status, 200)
        status, result = self.call("GET", "/api/devices/cam-1/maintenance/setup-ap-sync", token=admin_token)
        self.assertEqual(status, 200)
        self.assertEqual(result["maintenance"]["state"], "running")
        self.assertEqual(result["maintenance"]["details"]["message"], "Checking profile")
        self.assertEqual(self.call("POST", "/api/devices/cam-1/maintenance/setup-ap-sync", {
            "state": "mystery", "details": {},
        }, device_token)[0], 400)

    def test_pi_os_update_is_allowlisted_and_reports_authenticated_status(self):
        self.httpd.store.register({"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        device_token = "device-update-token"
        self.httpd.store.db.execute(
            "UPDATE devices SET token_hash=? WHERE id=?",
            (self.httpd.store.hash_token(device_token), "cam-1"),
        )
        self.httpd.store.db.commit()
        self.httpd.store.create_user("admin", "correct-horse", "admin")
        admin_token = self.httpd.store.create_session("admin")

        self.assertEqual(self.call("POST", "/api/devices/cam-1/commands", {"command": "update_os"}, admin_token)[0], 409)
        self.call("POST", "/api/devices/cam-1/heartbeat", {"agent_version": "0.1.9"}, device_token)
        self.assertEqual(self.call("POST", "/api/devices/cam-1/commands", {"command": "update_os"}, admin_token)[0], 202)
        self.assertEqual(self.call("POST", "/api/devices/cam-1/commands", {"command": "update_os"}, admin_token)[0], 409)
        self.assertEqual(self.call("POST", "/api/devices/cam-1/maintenance/os-update", {
            "state": "running", "details": {},
        })[0], 401)
        status, _ = self.call("POST", "/api/devices/cam-1/maintenance/os-update", {
            "state": "running", "details": {},
        }, device_token)
        self.assertEqual(status, 200)
        status, result = self.call("GET", "/api/devices/cam-1/maintenance/os-update", token=admin_token)
        self.assertEqual(status, 200)
        self.assertEqual(result["maintenance"]["state"], "running")
        self.assertEqual(self.call("POST", "/api/devices/cam-1/maintenance/os-update", {
            "state": "unknown", "details": {},
        }, device_token)[0], 400)

    def test_camera_live_urls_are_available(self):
        self.call("POST", "/api/devices", {"id": "cam-1", "kind": "camera", "name": "Saw 1"})
        status, live = self.call("GET", "/api/cameras/cam-1/live")
        self.assertEqual(status, 200)
        self.assertTrue(live["hls_url"].endswith("/cam-1/index.m3u8"))


if __name__ == "__main__":
    unittest.main()
