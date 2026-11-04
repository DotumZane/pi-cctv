"""Small SQLite persistence layer for the V1 management service."""

from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def serialized(method):
    """Serialize access to the shared SQLite connection used by HTTP threads."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Store:
    stale_after = timedelta(seconds=45)
    def __init__(self, path: str | Path = "data/cctv.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('camera', 'monitor')),
                name TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'offline',
                last_seen TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                token_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS assignments (
                monitor_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                layout TEXT NOT NULL DEFAULT 'single' CHECK(layout IN ('single', '2x2')),
                cameras TEXT NOT NULL DEFAULT '[]',
                outputs TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS device_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                command TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                adopted_device_id TEXT,
                issued_token TEXT
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)").fetchall()}
        if "token_hash" not in columns:
            self.db.execute("ALTER TABLE devices ADD COLUMN token_hash TEXT")
        assignment_columns = {row[1] for row in self.db.execute("PRAGMA table_info(assignments)").fetchall()}
        if "outputs" not in assignment_columns:
            self.db.execute("ALTER TABLE assignments ADD COLUMN outputs TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()

    @serialized
    def close(self) -> None:
        self.db.close()

    @serialized
    def device(self, device_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @serialized
    def device_token_valid(self, device_id: str, token: str) -> bool:
        device = self.device(device_id)
        return bool(device and device.get("token_hash") and secrets.compare_digest(device["token_hash"], self.hash_token(token)))

    @serialized
    def pending_register(self, device_id: str, name: str, ip_address: str, metadata: dict[str, Any]) -> dict[str, Any]:
        timestamp = now()
        self.db.execute(
            """INSERT INTO pending_devices(id, name, ip_address, metadata, first_seen, last_seen)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, ip_address=excluded.ip_address,
               metadata=excluded.metadata, last_seen=excluded.last_seen""",
            (device_id, name, ip_address, json.dumps(metadata), timestamp, timestamp),
        )
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM pending_devices WHERE id=?", (device_id,)).fetchone())

    @serialized
    def pending_devices(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM pending_devices WHERE adopted_device_id IS NULL ORDER BY first_seen").fetchall()]

    @serialized
    def adopt_pending(self, pending_id: str, kind: str, name: str, location: str) -> tuple[dict[str, Any], str] | None:
        pending = self.db.execute("SELECT * FROM pending_devices WHERE id=? AND adopted_device_id IS NULL", (pending_id,)).fetchone()
        if not pending:
            return None
        token = secrets.token_urlsafe(32)
        device_id = pending_id
        self.db.execute(
            """INSERT INTO devices(id, kind, name, location, ip_address, status, last_seen, metadata, token_hash)
               VALUES(?, ?, ?, ?, ?, 'online', ?, ?, ?)""",
            (device_id, kind, name, location, pending["ip_address"], now(), pending["metadata"], self.hash_token(token)),
        )
        self.db.execute("UPDATE pending_devices SET adopted_device_id=?, issued_token=? WHERE id=?", (device_id, token, pending_id))
        self.db.commit()
        return self.device(device_id), token  # type: ignore[return-value]

    @serialized
    def claim_adoption(self, pending_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT d.*, p.issued_token FROM devices d JOIN pending_devices p ON p.adopted_device_id=d.id WHERE p.id=? AND p.issued_token IS NOT NULL",
            (pending_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        self.db.execute("UPDATE pending_devices SET issued_token=NULL WHERE id=?", (pending_id,))
        self.db.commit()
        return result

    @serialized
    def delete_device(self, device_id: str) -> bool:
        result = self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self.db.commit()
        return result.rowcount > 0

    @serialized
    def devices(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.db.execute("SELECT * FROM devices WHERE kind = ? ORDER BY name", (kind,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM devices ORDER BY kind, name").fetchall()
        current = datetime.now(timezone.utc)
        devices = []
        for row in rows:
            device = dict(row)
            if device["last_seen"]:
                try:
                    last_seen = datetime.fromisoformat(device["last_seen"])
                    if current - last_seen > self.stale_after:
                        device["status"] = "offline"
                except ValueError:
                    device["status"] = "offline"
            else:
                device["status"] = "offline"
            devices.append(device)
        return devices

    @serialized
    def health_summary(self) -> dict[str, Any]:
        devices = self.devices()
        return {
            "total": len(devices),
            "online": sum(device["status"] == "online" for device in devices),
            "offline": sum(device["status"] == "offline" for device in devices),
            "cameras": sum(device["kind"] == "camera" for device in devices),
            "monitors": sum(device["kind"] == "monitor" for device in devices),
        }

    @serialized
    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload["id"])
        self.db.execute(
            """INSERT INTO devices(id, kind, name, location, ip_address, status, last_seen, metadata)
               VALUES(?, ?, ?, ?, ?, 'online', ?, ?)
               ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name,
               location=excluded.location, ip_address=excluded.ip_address, status='online',
               last_seen=excluded.last_seen, metadata=excluded.metadata""",
            (device_id, payload["kind"], payload["name"], payload.get("location", ""),
             payload.get("ip_address", ""), now(), payload.get("metadata", "{}")),
        )
        self.db.commit()
        return self.device(device_id)  # type: ignore[return-value]

    @serialized
    def heartbeat(self, device_id: str, ip_address: str = "", metadata: str = "{}") -> dict[str, Any] | None:
        existing = self.device(device_id)
        try:
            merged_metadata = json.loads(existing["metadata"] or "{}") if existing else {}
            merged_metadata.update(json.loads(metadata or "{}"))
            metadata = json.dumps(merged_metadata)
        except json.JSONDecodeError:
            pass
        self.db.execute(
            "UPDATE devices SET status='online', last_seen=?, ip_address=COALESCE(NULLIF(?, ''), ip_address), metadata=? WHERE id=?",
            (now(), ip_address, metadata, device_id),
        )
        self.db.commit()
        return self.device(device_id)

    @serialized
    def update_settings(self, device_id: str, settings: dict[str, Any]) -> dict[str, Any] | None:
        device = self.device(device_id)
        if not device:
            return None
        try:
            metadata = json.loads(device["metadata"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        metadata["settings"] = settings
        self.db.execute("UPDATE devices SET metadata=? WHERE id=?", (json.dumps(metadata), device_id))
        self.db.commit()
        return self.device(device_id)

    @serialized
    def assignment(self, monitor_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM assignments WHERE monitor_id = ?", (monitor_id,)).fetchone()
        return dict(row) if row else None

    @serialized
    def set_assignment(self, monitor_id: str, layout: str, cameras: list[str], outputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        updated = now()
        self.db.execute(
            """INSERT INTO assignments(monitor_id, layout, cameras, outputs, updated_at) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(monitor_id) DO UPDATE SET layout=excluded.layout, cameras=excluded.cameras,
               outputs=excluded.outputs, updated_at=excluded.updated_at""",
            (monitor_id, layout, __import__("json").dumps(cameras), __import__("json").dumps(outputs or []), updated),
        )
        self.db.commit()
        return self.assignment(monitor_id)  # type: ignore[return-value]

    @serialized
    def all_assignments(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM assignments ORDER BY monitor_id").fetchall()]

    @serialized
    def audit(self, action: str, target: str, source: str = "", details: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_log(created_at, action, target, source, details) VALUES(?, ?, ?, ?, ?)",
            (now(), action, target, source, json.dumps(details or {})),
        )
        self.db.commit()

    @serialized
    def audit_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        return [dict(row) for row in self.db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    @serialized
    def queue_command(self, device_id: str, command: str) -> dict[str, Any]:
        cursor = self.db.execute(
            "INSERT INTO device_commands(device_id, command, created_at) VALUES(?, ?, ?)",
            (device_id, command, now()),
        )
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM device_commands WHERE id = ?", (cursor.lastrowid,)).fetchone())

    @serialized
    def consume_commands(self, device_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM device_commands WHERE device_id=? AND consumed_at IS NULL ORDER BY id", (device_id,)).fetchall()
        if rows:
            self.db.execute("UPDATE device_commands SET consumed_at=? WHERE device_id=? AND consumed_at IS NULL", (now(), device_id))
            self.db.commit()
        return [dict(row) for row in rows]
