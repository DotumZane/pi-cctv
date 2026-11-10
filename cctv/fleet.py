"""Persistent, sequential Pi OS fleet maintenance jobs."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .store import Store


FLEET_OS_UPDATE_KEY = "fleet_os_update"
FLEET_AGENT_UPDATE_KEY = "fleet_agent_update"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FleetOsUpdateCoordinator:
    """Run one Pi OS update at a time, independently of a browser session."""

    def __init__(self, store: Store, agent_version: str, poll_seconds: float = 5.0, timeout_seconds: int = 1800,
                 execution_lock: threading.Lock | None = None) -> None:
        self.store = store
        self.agent_version = agent_version
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.execution_lock = execution_lock or threading.Lock()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        existing = self.status()
        if existing and existing.get("state") in {"queued", "running"}:
            self._launch()

    def status(self) -> dict | None:
        return self.store.system_setting(FLEET_OS_UPDATE_KEY)

    def start(self) -> tuple[dict, bool]:
        with self._lock:
            current = self.status()
            if current and current.get("state") in {"queued", "running"}:
                return current, False
            targets = []
            for device in self.store.devices():
                if device.get("status") != "online":
                    continue
                try:
                    metadata = json.loads(device.get("metadata", "{}"))
                except (TypeError, ValueError):
                    metadata = {}
                if metadata.get("agent_version") != self.agent_version:
                    continue
                previous = self.store.maintenance_run(device["id"], "os_update")
                if previous and previous.get("state") in {"queued", "running"}:
                    continue
                targets.append({"device_id": device["id"], "name": device["name"], "state": "waiting"})
            if not targets:
                raise ValueError("No online Pis with the current CCTV agent are available for Pi OS updates")
            timestamp = _now()
            job = {
                "id": uuid.uuid4().hex,
                "state": "queued",
                "target_agent_version": self.agent_version,
                "requested_at": timestamp,
                "started_at": None,
                "finished_at": None,
                "targets": targets,
                "current_index": 0,
                "completed": 0,
                "error": "",
            }
            self._save(job)
            self._launch()
            return job, True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _save(self, job: dict) -> None:
        self.store.set_system_setting(FLEET_OS_UPDATE_KEY, job)

    def _launch(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fleet-pi-os-update", daemon=True)
        self._thread.start()

    def _set_terminal(self, job: dict, state: str, error: str = "") -> None:
        job["state"] = state
        job["error"] = error
        job["finished_at"] = _now()
        self._save(job)

    def _command_for_target(self, target: dict, job: dict) -> dict | None:
        if target.get("command_id"):
            return next((row for row in self.store.command_history(target["device_id"], 100)
                         if row.get("id") == target["command_id"]), None)
        for row in self.store.command_history(target["device_id"], 100):
            if row.get("command") == "update_os" and row.get("created_at", "") >= job["requested_at"]:
                target["command_id"] = row["id"]
                self._save(job)
                return row
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.execution_lock.acquire(timeout=0.5):
                try:
                    self._run_job()
                finally:
                    self.execution_lock.release()
                return

    def _run_job(self) -> None:
        job = self.status()
        if not job or job.get("state") not in {"queued", "running"}:
            return
        job["state"] = "running"
        job["started_at"] = job.get("started_at") or _now()
        self._save(job)
        while not self._stop.is_set():
            index = int(job.get("current_index", 0))
            targets = job.get("targets", [])
            if index >= len(targets):
                self._set_terminal(job, "succeeded")
                return
            target = targets[index]
            device = self.store.device(target["device_id"])
            online_ids = {row["id"] for row in self.store.devices() if row.get("status") == "online"}
            if not device or target["device_id"] not in online_ids:
                target["state"] = "skipped"
                target["message"] = "Pi is offline"
                job["current_index"] += 1
                self._save(job)
                continue
            try:
                metadata = json.loads(device.get("metadata", "{}"))
            except (TypeError, ValueError):
                metadata = {}
            if metadata.get("agent_version") != self.agent_version:
                target["state"] = "skipped"
                target["message"] = "CCTV agent version changed; update the agent first"
                job["current_index"] += 1
                self._save(job)
                continue

            maintenance = self.store.maintenance_run(target["device_id"], "os_update")
            command = self._command_for_target(target, job)
            if not command and (not maintenance or maintenance.get("state") not in {"queued", "running"}):
                target["state"] = "queueing"
                target["queued_at"] = _now()
                self._save(job)
                self.store.set_maintenance_run(target["device_id"], "os_update", "queued", {})
                command = self.store.queue_command(target["device_id"], "update_os")
                target["command_id"] = command["id"]
                target["state"] = "queued"
                self._save(job)
            elif not command and maintenance and maintenance.get("state") in {"queued", "running"}:
                # Recover a command that was written just before a server restart.
                command = self._command_for_target(target, job)
                if not command:
                    if maintenance.get("state") == "running":
                        target["state"] = "running"
                        target.setdefault("queued_at", maintenance.get("updated_at") or _now())
                    else:
                        target["state"] = "queueing"
                        target.setdefault("queued_at", _now())
                        self._save(job)
                        self.store.set_maintenance_run(target["device_id"], "os_update", "queued", {})
                        command = self.store.queue_command(target["device_id"], "update_os")
                        target["command_id"] = command["id"]
                        target["state"] = "queued"
                        self._save(job)

            while not self._stop.is_set():
                maintenance = self.store.maintenance_run(target["device_id"], "os_update")
                state = maintenance.get("state") if maintenance else ""
                if state == "succeeded":
                    target["state"] = "succeeded"
                    target["updated_at"] = maintenance.get("updated_at")
                    target["details"] = maintenance.get("details", {})
                    job["completed"] = int(job.get("completed", 0)) + 1
                    job["current_index"] += 1
                    self._save(job)
                    break
                if state == "failed":
                    target["state"] = "failed"
                    target["updated_at"] = maintenance.get("updated_at")
                    target["details"] = maintenance.get("details", {})
                    self._set_terminal(job, "failed", f"{target['name']} reported a Pi OS update failure")
                    return
                queued_at = target.get("queued_at") or (maintenance or {}).get("updated_at") or _now()
                try:
                    expired = datetime.now(timezone.utc) - datetime.fromisoformat(queued_at) > timedelta(seconds=self.timeout_seconds)
                except (TypeError, ValueError):
                    expired = False
                if expired:
                    target["state"] = "failed"
                    target["message"] = "No completion report arrived within 30 minutes"
                    self._set_terminal(job, "failed", f"{target['name']} did not report completion within 30 minutes")
                    return
                if self._stop.wait(self.poll_seconds):
                    return


class FleetAgentUpdateCoordinator:
    """Update outdated Pis one by one and persist progress outside the browser."""

    def __init__(self, store: Store, agent_version: str, poll_seconds: float = 3.0, timeout_seconds: int = 180,
                 execution_lock: threading.Lock | None = None) -> None:
        self.store = store
        self.agent_version = agent_version
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.execution_lock = execution_lock or threading.Lock()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        existing = self.status()
        if existing and existing.get("state") in {"queued", "running"}:
            self._launch()

    def status(self) -> dict | None:
        return self.store.system_setting(FLEET_AGENT_UPDATE_KEY)

    def start(self) -> tuple[dict, bool]:
        with self._lock:
            current = self.status()
            if current and current.get("state") in {"queued", "running"}:
                return current, False
            targets = []
            for device in self.store.devices():
                if device.get("status") != "online":
                    continue
                try:
                    version = json.loads(device.get("metadata", "{}")).get("agent_version", "")
                except (TypeError, ValueError):
                    version = ""
                if version != self.agent_version:
                    pending_update = any(
                        command.get("command") == "update" and not command.get("consumed_at")
                        for command in self.store.command_history(device["id"], 100)
                    )
                    if not pending_update:
                        targets.append({"device_id": device["id"], "name": device["name"], "state": "waiting"})
            if not targets:
                raise ValueError("No online Pis need a CCTV software update")
            job = {
                "id": uuid.uuid4().hex,
                "state": "queued",
                "target_agent_version": self.agent_version,
                "requested_at": _now(),
                "started_at": None,
                "finished_at": None,
                "targets": targets,
                "current_index": 0,
                "completed": 0,
                "error": "",
            }
            self._save(job)
            self._launch()
            return job, True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _save(self, job: dict) -> None:
        self.store.set_system_setting(FLEET_AGENT_UPDATE_KEY, job)

    def _launch(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fleet-agent-update", daemon=True)
        self._thread.start()

    def _terminal(self, job: dict, state: str, error: str = "") -> None:
        job["state"] = state
        job["error"] = error
        job["finished_at"] = _now()
        self._save(job)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.execution_lock.acquire(timeout=0.5):
                try:
                    self._run_job()
                finally:
                    self.execution_lock.release()
                return

    def _run_job(self) -> None:
        job = self.status()
        if not job or job.get("state") not in {"queued", "running"}:
            return
        job["state"] = "running"
        job["started_at"] = job.get("started_at") or _now()
        self._save(job)
        while not self._stop.is_set():
            index = int(job.get("current_index", 0))
            targets = job.get("targets", [])
            if index >= len(targets):
                self._terminal(job, "succeeded")
                return
            target = targets[index]
            device = self.store.device(target["device_id"])
            online = next((item for item in self.store.devices() if item["id"] == target["device_id"] and item["status"] == "online"), None)
            if not device or (not online and not target.get("command_id")):
                target["state"] = "skipped"
                target["message"] = "Pi is offline"
                job["current_index"] += 1
                self._save(job)
                continue
            try:
                version = json.loads(device.get("metadata", "{}")).get("agent_version", "")
            except (TypeError, ValueError):
                version = ""
            if online and version == self.agent_version:
                target["state"] = "succeeded"
                target["updated_at"] = device.get("last_seen")
                job["completed"] = int(job.get("completed", 0)) + 1
                job["current_index"] += 1
                self._save(job)
                continue

            command = None
            if target.get("command_id"):
                command = next((item for item in self.store.command_history(target["device_id"], 100)
                                if item.get("id") == target["command_id"]), None)
            if not command:
                command = next((item for item in self.store.command_history(target["device_id"], 100)
                                if item.get("command") == "update" and item.get("created_at", "") >= job["requested_at"]), None)
                if command:
                    target["command_id"] = command["id"]
                    target.setdefault("queued_at", command.get("created_at") or _now())
                    self._save(job)
            if not command:
                target["state"] = "queueing"
                target["queued_at"] = _now()
                self._save(job)
                command = self.store.queue_command(target["device_id"], "update")
                target["command_id"] = command["id"]
                target["state"] = "queued"
                self._save(job)

            while not self._stop.is_set():
                current = next((item for item in self.store.devices() if item["id"] == target["device_id"]), None)
                if current and current.get("status") == "online":
                    try:
                        version = json.loads(current.get("metadata", "{}")).get("agent_version", "")
                    except (TypeError, ValueError):
                        version = ""
                    if version == self.agent_version:
                        target["state"] = "succeeded"
                        target["updated_at"] = current.get("last_seen")
                        job["completed"] = int(job.get("completed", 0)) + 1
                        job["current_index"] += 1
                        self._save(job)
                        break
                    target["state"] = "running" if command.get("consumed_at") else "queued"
                queued_at = target.get("queued_at") or _now()
                try:
                    expired = datetime.now(timezone.utc) - datetime.fromisoformat(queued_at) > timedelta(seconds=self.timeout_seconds)
                except (TypeError, ValueError):
                    expired = False
                if expired:
                    target["state"] = "failed"
                    target["message"] = f"Pi did not report agent {self.agent_version} within 3 minutes"
                    self._terminal(job, "failed", f"{target['name']} did not return on agent {self.agent_version} within 3 minutes")
                    return
                self._save(job)
                if self._stop.wait(self.poll_seconds):
                    return
