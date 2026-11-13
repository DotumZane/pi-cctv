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
    session_lifetime = timedelta(hours=12)
    log_history_retention = timedelta(days=7)
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
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS maintenance_runs (
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                task TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(device_id, task)
            );
            CREATE TABLE IF NOT EXISTS pending_devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                adopted_device_id TEXT,
                issued_token TEXT,
                enrollment_secret_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('admin', 'operator', 'viewer')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)").fetchall()}
        if "token_hash" not in columns:
            self.db.execute("ALTER TABLE devices ADD COLUMN token_hash TEXT")
        pending_columns = {row[1] for row in self.db.execute("PRAGMA table_info(pending_devices)").fetchall()}
        if "enrollment_secret_hash" not in pending_columns:
            self.db.execute("ALTER TABLE pending_devices ADD COLUMN enrollment_secret_hash TEXT")
        assignment_columns = {row[1] for row in self.db.execute("PRAGMA table_info(assignments)").fetchall()}
        if "outputs" not in assignment_columns:
            self.db.execute("ALTER TABLE assignments ADD COLUMN outputs TEXT NOT NULL DEFAULT '[]'")
        command_columns = {row[1] for row in self.db.execute("PRAGMA table_info(device_commands)").fetchall()}
        if "payload" not in command_columns:
            self.db.execute("ALTER TABLE device_commands ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()

    @serialized
    def close(self) -> None:
        self.db.close()

    @serialized
    def system_setting(self, key: str) -> Any | None:
        row = self.db.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    @serialized
    def set_system_setting(self, key: str, value: Any, *, only_if_missing: bool = False) -> None:
        if only_if_missing:
            self.db.execute(
                "INSERT OR IGNORE INTO system_settings(key, value) VALUES(?, ?)",
                (key, json.dumps(value)),
            )
        else:
            self.db.execute(
                "INSERT INTO system_settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
        self.db.commit()

    @serialized
    def device(self, device_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def hash_password(password: str, salt: bytes | None = None) -> str:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return f"pbkdf2_sha256$260000${salt.hex()}${digest.hex()}"

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
            if algorithm != "pbkdf2_sha256":
                return False
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
            return secrets.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False

    @serialized
    def user_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    @serialized
    def users(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT username, role, created_at FROM users ORDER BY username").fetchall()
        return [{"username": row["username"], "role": row["role"], "created_at": row["created_at"]} for row in rows]

    @serialized
    def create_user(self, username: str, password: str, role: str = "admin") -> dict[str, Any]:
        username = username.strip()
        if not username or len(username) > 80 or len(password) < 8 or role not in {"admin", "operator", "viewer"}:
            raise ValueError("username, password, or role is invalid")
        self.db.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES(?, ?, ?, ?)", (username, self.hash_password(password), role, now()))
        self.db.commit()
        return {"username": username, "role": role}

    @serialized
    def set_password(self, username: str, password: str, preserve_session_token: str = "") -> None:
        username = username.strip()
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("password must be 8–128 characters")
        result = self.db.execute("UPDATE users SET password_hash=? WHERE username=?", (self.hash_password(password), username))
        if result.rowcount == 0:
            raise ValueError("user not found")
        if preserve_session_token:
            self.db.execute("DELETE FROM sessions WHERE username=? AND token_hash!=?", (username, self.hash_token(preserve_session_token)))
        else:
            self.db.execute("DELETE FROM sessions WHERE username=?", (username,))
        self.db.commit()

    @serialized
    def delete_user(self, username: str) -> None:
        self.db.execute("DELETE FROM users WHERE username=?", (username,))
        self.db.commit()

    @serialized
    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT username, password_hash, role FROM users WHERE username=?", (username.strip(),)).fetchone()
        if not row or not self.verify_password(password, row["password_hash"]):
            return None
        return {"username": row["username"], "role": row["role"]}

    @serialized
    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        self.db.execute("INSERT INTO sessions(token_hash, username, created_at, expires_at) VALUES(?, ?, ?, ?)", (self.hash_token(token), username, created.isoformat(), (created + self.session_lifetime).isoformat()))
        self.db.commit()
        return token

    @serialized
    def session_user(self, token: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT u.username, u.role, s.expires_at FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=?", (self.hash_token(token),)).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (self.hash_token(token),))
            self.db.commit()
            return None
        return {"username": row["username"], "role": row["role"]}

    @serialized
    def revoke_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (self.hash_token(token),))
        self.db.commit()

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
    def pending_register_discovered(self, device_id: str, name: str, ip_address: str, metadata: dict[str, Any], secret: str) -> dict[str, Any]:
        timestamp = now()
        self.db.execute(
            """INSERT INTO pending_devices(id, name, ip_address, metadata, first_seen, last_seen, enrollment_secret_hash)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, ip_address=excluded.ip_address,
               metadata=excluded.metadata, last_seen=excluded.last_seen
               WHERE pending_devices.adopted_device_id IS NULL
                 AND pending_devices.enrollment_secret_hash=excluded.enrollment_secret_hash""",
            (device_id, name, ip_address, json.dumps(metadata), timestamp, timestamp, self.hash_token(secret)),
        )
        row = self.db.execute("SELECT * FROM pending_devices WHERE id=?", (device_id,)).fetchone()
        return dict(row) if row and row["enrollment_secret_hash"] == self.hash_token(secret) else {}

    @serialized
    def discovered_adoption_claim(self, device_id: str, secret: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT d.*, p.issued_token FROM devices d JOIN pending_devices p ON p.adopted_device_id=d.id
               WHERE p.id=? AND p.enrollment_secret_hash=? AND p.issued_token IS NOT NULL""",
            (device_id, self.hash_token(secret)),
        ).fetchone()
        return dict(row) if row else None

    @serialized
    def pending_secret_valid(self, device_id: str, secret: str) -> bool:
        row = self.db.execute("SELECT enrollment_secret_hash FROM pending_devices WHERE id=?", (device_id,)).fetchone()
        return bool(row and row["enrollment_secret_hash"] and secrets.compare_digest(row["enrollment_secret_hash"], self.hash_token(secret)))

    @serialized
    def discard_unadopted_pending(self, device_id: str, secret: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM pending_devices WHERE id=? AND adopted_device_id IS NULL AND enrollment_secret_hash=?",
            (device_id, self.hash_token(secret)),
        )
        self.db.commit()
        return cursor.rowcount > 0

    @serialized
    def pending_devices(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT id, name, ip_address, metadata, first_seen, last_seen FROM pending_devices WHERE adopted_device_id IS NULL ORDER BY first_seen"
        ).fetchall()]

    @serialized
    def adopt_pending(
        self, pending_id: str, kind: str, name: str, location: str,
        replace_device_id: str = "",
    ) -> tuple[dict[str, Any], str] | None:
        pending = self.db.execute("SELECT * FROM pending_devices WHERE id=? AND adopted_device_id IS NULL", (pending_id,)).fetchone()
        if not pending:
            return None
        token = secrets.token_urlsafe(32)
        device_id = pending_id
        replacement = None
        if replace_device_id:
            if kind != "camera" or replace_device_id == pending_id:
                return None
            replacement = next((item for item in self.devices() if item["id"] == replace_device_id), None)
            if not replacement or replacement["kind"] != "camera" or replacement["status"] != "offline":
                return None
            device_id = replace_device_id
        existing = self.device(device_id)
        if existing:
            try:
                metadata = json.loads(existing.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            if replacement:
                try:
                    pending_metadata = json.loads(pending["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    pending_metadata = {}
                old_settings = metadata.get("settings", {})
                metadata.update(pending_metadata)
                metadata["settings"] = old_settings
                name = existing["name"]
                location = existing["location"]
                # Never execute commands queued for the retired hardware on its replacement.
                self.db.execute(
                    "DELETE FROM device_commands WHERE device_id=? AND consumed_at IS NULL",
                    (device_id,),
                )
            self.db.execute(
                """UPDATE devices SET kind=?, name=?, location=?, ip_address=?, status='online',
                   last_seen=?, token_hash=?, metadata=? WHERE id=?""",
                (kind, name, location, pending["ip_address"], now(), self.hash_token(token), json.dumps(metadata), device_id),
            )
        else:
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
        self.db.execute("DELETE FROM assignments WHERE monitor_id = ?", (device_id,))
        assignments = self.db.execute("SELECT monitor_id, layout, cameras, outputs FROM assignments").fetchall()
        for assignment in assignments:
            try:
                cameras = json.loads(assignment["cameras"] or "[]")
                outputs = json.loads(assignment["outputs"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            updated_cameras = [camera for camera in cameras if camera != device_id] if isinstance(cameras, list) else []
            if isinstance(outputs, list):
                for output in outputs:
                    if isinstance(output, dict) and isinstance(output.get("cameras"), list):
                        output["cameras"] = [camera for camera in output["cameras"] if camera != device_id]
            if updated_cameras != cameras or outputs != json.loads(assignment["outputs"] or "[]"):
                self.db.execute(
                    "UPDATE assignments SET cameras = ?, outputs = ?, updated_at = ? WHERE monitor_id = ?",
                    (json.dumps(updated_cameras), json.dumps(outputs), now(), assignment["monitor_id"]),
                )
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
    def update_profile(self, device_id: str, name: str, location: str) -> dict[str, Any] | None:
        cursor = self.db.execute(
            "UPDATE devices SET name=?, location=? WHERE id=?",
            (name, location, device_id),
        )
        self.db.commit()
        return self.device(device_id) if cursor.rowcount else None

    @serialized
    def heartbeat(self, device_id: str, ip_address: str = "", metadata: str = "{}") -> dict[str, Any] | None:
        existing = self.device(device_id)
        received_at = now()
        try:
            merged_metadata = json.loads(existing["metadata"] or "{}") if existing else {}
            heartbeat_metadata = json.loads(metadata or "{}")
            if isinstance(heartbeat_metadata, dict) and isinstance(heartbeat_metadata.get("recent_logs"), list):
                history = merged_metadata.get("server_log_history", [])
                if not isinstance(history, list):
                    history = []
                cutoff = datetime.fromisoformat(received_at) - self.log_history_retention
                retained = []
                for entry in history:
                    if not isinstance(entry, dict) or not isinstance(entry.get("line"), str):
                        continue
                    try:
                        if datetime.fromisoformat(str(entry.get("received_at"))) >= cutoff:
                            retained.append(entry)
                    except (TypeError, ValueError):
                        continue
                known_lines = {entry["line"] for entry in retained[-5000:]}
                for line in heartbeat_metadata["recent_logs"]:
                    if isinstance(line, str) and line not in known_lines:
                        retained.append({"received_at": received_at, "line": line})
                        known_lines.add(line)
                heartbeat_metadata.pop("recent_logs", None)
                merged_metadata["server_log_history"] = retained
            merged_metadata.update(heartbeat_metadata)
            metadata = json.dumps(merged_metadata)
        except (json.JSONDecodeError, TypeError):
            pass
        self.db.execute(
            "UPDATE devices SET status='online', last_seen=?, ip_address=COALESCE(NULLIF(?, ''), ip_address), metadata=? WHERE id=?",
            (received_at, ip_address, metadata, device_id),
        )
        self.db.execute(
            "UPDATE pending_devices SET enrollment_secret_hash=NULL, issued_token=NULL WHERE adopted_device_id=?",
            (device_id,),
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
    def queue_command(self, device_id: str, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        cursor = self.db.execute(
            "INSERT INTO device_commands(device_id, command, payload, created_at) VALUES(?, ?, ?, ?)",
            (device_id, command, json.dumps(payload or {}), now()),
        )
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM device_commands WHERE id = ?", (cursor.lastrowid,)).fetchone())

    @serialized
    def consume_commands(self, device_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM device_commands WHERE device_id=? AND consumed_at IS NULL ORDER BY id", (device_id,)).fetchall()
        if rows:
            self.db.execute("UPDATE device_commands SET consumed_at=? WHERE device_id=? AND consumed_at IS NULL", (now(), device_id))
            self.db.commit()
        commands = [dict(row) for row in rows]
        for command in commands:
            try:
                command["payload"] = json.loads(command.get("payload") or "{}")
            except (json.JSONDecodeError, TypeError):
                command["payload"] = {}
        return commands

    @serialized
    def command_history(self, device_id: str, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM device_commands WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()]

    @serialized
    def maintenance_run(self, device_id: str, task: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT task, state, updated_at, details FROM maintenance_runs WHERE device_id=? AND task=?",
            (device_id, task),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["details"] = json.loads(result["details"] or "{}")
        return result

    @serialized
    def set_maintenance_run(self, device_id: str, task: str, state: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        if state not in {"queued", "running", "succeeded", "failed"}:
            raise ValueError("invalid maintenance state")
        updated_at = now()
        self.db.execute(
            "INSERT INTO maintenance_runs(device_id, task, state, updated_at, details) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id, task) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at, details=excluded.details",
            (device_id, task, state, updated_at, json.dumps(details or {})),
        )
        self.db.commit()
        return self.maintenance_run(device_id, task) or {}
