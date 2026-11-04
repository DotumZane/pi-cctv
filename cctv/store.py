"""Small SQLite persistence layer for the V1 management service."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = "data/cctv.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS assignments (
                monitor_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                layout TEXT NOT NULL DEFAULT 'single' CHECK(layout IN ('single', '2x2')),
                cameras TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def device(self, device_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def devices(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.db.execute("SELECT * FROM devices WHERE kind = ? ORDER BY name", (kind,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM devices ORDER BY kind, name").fetchall()
        return [dict(row) for row in rows]

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

    def heartbeat(self, device_id: str, ip_address: str = "", metadata: str = "{}") -> dict[str, Any] | None:
        self.db.execute(
            "UPDATE devices SET status='online', last_seen=?, ip_address=COALESCE(NULLIF(?, ''), ip_address), metadata=? WHERE id=?",
            (now(), ip_address, metadata, device_id),
        )
        self.db.commit()
        return self.device(device_id)

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

    def assignment(self, monitor_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM assignments WHERE monitor_id = ?", (monitor_id,)).fetchone()
        return dict(row) if row else None

    def set_assignment(self, monitor_id: str, layout: str, cameras: list[str]) -> dict[str, Any]:
        updated = now()
        self.db.execute(
            """INSERT INTO assignments(monitor_id, layout, cameras, updated_at) VALUES(?, ?, ?, ?)
               ON CONFLICT(monitor_id) DO UPDATE SET layout=excluded.layout, cameras=excluded.cameras,
               updated_at=excluded.updated_at""",
            (monitor_id, layout, __import__("json").dumps(cameras), updated),
        )
        self.db.commit()
        return self.assignment(monitor_id)  # type: ignore[return-value]

    def all_assignments(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM assignments ORDER BY monitor_id").fetchall()]
