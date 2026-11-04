import json
import tempfile
import threading
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

    def call(self, method, path, payload=None, token=""):
        connection = HTTPConnection(*self.base)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
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
        status, index = self.call("GET", "/api/recordings?camera_id=cam-01")
        self.assertEqual(status, 200)
        self.assertEqual(index["recordings"][0]["path"], "cam-01/2026-11-04/06-00-00.mkv")


if __name__ == "__main__":
    unittest.main()
