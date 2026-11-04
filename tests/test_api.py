import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from cctv.server import Handler
from cctv.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.store = Store(":memory:")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = ("127.0.0.1", self.httpd.server_port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.store.close()

    def call(self, method, path, payload=None):
        connection = HTTPConnection(*self.base)
        data = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, data, {"Content-Type": "application/json"} if data else {})
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


if __name__ == "__main__":
    unittest.main()
