import json
import threading
import time
import unittest
from datetime import datetime, timezone

from cctv.fleet import FleetAgentUpdateCoordinator, FleetOsUpdateCoordinator
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

    def test_os_update_waits_for_completion_after_pi_temporarily_goes_offline(self):
        coordinator = FleetOsUpdateCoordinator(self.store, "0.1.4", poll_seconds=0.01, timeout_seconds=1)
        coordinator._launch = lambda: None
        job, _ = coordinator.start()
        command = self.store.queue_command("cam-1", "update_os")
        self.store.set_maintenance_run("cam-1", "os_update", "running", {})
        job["targets"][0].update({
            "command_id": command["id"],
            "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "state": "running",
        })
        coordinator._save(job)

        original_devices = self.store.devices
        self.store.devices = lambda kind=None: [
            {**device, "status": "offline"} for device in original_devices(kind)
        ]
        timer = threading.Timer(
            0.03,
            lambda: self.store.set_maintenance_run("cam-1", "os_update", "succeeded", {"reboot_required": False}),
        )
        try:
            timer.start()
            coordinator._run_job()
            result = coordinator.status()
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(result["targets"][0]["state"], "succeeded")
            self.assertEqual(result["completed"], 1)
        finally:
            timer.cancel()
            self.store.devices = original_devices
            coordinator.stop()

    def test_agent_update_worker_queues_and_waits_for_latest_agent_heartbeat(self):
        coordinator = FleetAgentUpdateCoordinator(self.store, "0.1.5", poll_seconds=0.01, timeout_seconds=1)
        queue_command = self.store.queue_command

        def queue_and_report(device_id, command):
            queued = queue_command(device_id, command)
            self.store.heartbeat(device_id, "192.0.2.1", json.dumps({"agent_version": "0.1.5"}))
            return queued

        self.store.queue_command = queue_and_report
        try:
            coordinator.start()
            deadline = time.monotonic() + 2
            while coordinator.status()["state"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.01)
            job = coordinator.status()
            self.assertEqual(job["state"], "succeeded")
            self.assertEqual(job["completed"], 1)
            self.assertEqual(self.store.command_history("cam-1", 1)[0]["command"], "update")
        finally:
            coordinator.stop()

    def test_fleet_agent_and_os_jobs_share_the_same_serial_execution_lock(self):
        lock = threading.Lock()
        agent = FleetAgentUpdateCoordinator(self.store, "0.1.5", execution_lock=lock)
        operating_system = FleetOsUpdateCoordinator(self.store, "0.1.4", execution_lock=lock)
        try:
            self.assertIs(agent.execution_lock, operating_system.execution_lock)
        finally:
            agent.stop()
            operating_system.stop()


if __name__ == "__main__":
    unittest.main()
