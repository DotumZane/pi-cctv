import json
import unittest

from cctv.fleet import FleetOsUpdateCoordinator
from cctv.store import Store


class FleetOsUpdateTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.pending_register("cam-1", "Camera 1", "192.0.2.1", {"agent_version": "0.1.4"})
        self.store.adopt_pending("cam-1", "camera", "Camera 1", "")
        self.store.heartbeat("cam-1", "192.0.2.1", json.dumps({"agent_version": "0.1.4"}))
        self.coordinator = FleetOsUpdateCoordinator(self.store, "0.1.4")
        self.coordinator._launch = lambda: None

    def tearDown(self):
        self.coordinator.stop()
        self.store.close()

    def test_start_persists_eligible_devices_and_rejects_duplicate_job(self):
        job, started = self.coordinator.start()
        self.assertTrue(started)
        self.assertEqual(job["state"], "queued")
        self.assertEqual([target["device_id"] for target in job["targets"]], ["cam-1"])
        self.assertEqual(self.coordinator.status()["id"], job["id"])

        same_job, started = self.coordinator.start()
        self.assertFalse(started)
        self.assertEqual(same_job["id"], job["id"])

    def test_start_skips_offline_or_outdated_agent_devices(self):
        self.store.pending_register("cam-2", "Camera 2", "192.0.2.2", {"agent_version": "0.1.3"})
        self.store.adopt_pending("cam-2", "camera", "Camera 2", "")
        with self.assertRaisesRegex(ValueError, "No online Pis"):
            self.store.delete_device("cam-1")
            self.coordinator.start()


if __name__ == "__main__":
    unittest.main()
