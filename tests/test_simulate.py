import unittest

from cctv.simulate import provision


class SimulationTests(unittest.TestCase):
    def test_virtual_fleet_provisioning_shape(self):
        # Exercise the request shape without requiring a server.
        calls = []
        import cctv.simulate as simulate
        original = simulate.request
        simulate.request = lambda base, method, path, payload, token="": calls.append((method, path, payload)) or ({"ok": True} if path == "/api/health" else {})
        try:
            result = provision("http://server", 2, 3)
        finally:
            simulate.request = original
        self.assertEqual(result, {"ok": True})
        self.assertEqual(sum(path == "/api/devices" for _, path, _ in calls), 5)
        self.assertEqual(sum(method == "PUT" for method, _, _ in calls), 3)
        self.assertEqual(calls[-1][1], "/api/health")
