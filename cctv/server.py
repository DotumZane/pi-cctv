"""Dependency-free V1 management API and browser dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __agent_version__, __version__
from .fleet import FleetAgentUpdateCoordinator, FleetOsUpdateCoordinator
from .recording import RecorderSupervisor
from .store import Store


ROOT = Path(__file__).parent
DEFAULT_NVR_SETTINGS = {
    "recording_enabled": True,
    "retention_hours": 24,
    "segment_seconds": 900,
    "storage_limit_gb": 500,
    "camera_recording": {},
}
PLAYBACK_CACHE_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "PiCCTV/0.1"

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def send_json(self, value: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def session_token(self) -> str:
        cookies = self.headers.get("Cookie", "").split(";")
        for cookie in cookies:
            name, _, value = cookie.strip().partition("=")
            if name == "cctv_session":
                return value
        return ""

    def session_user(self) -> dict | None:
        token = self.session_token()
        if not token:
            supplied = self.headers.get("Authorization", "")
            if supplied.startswith("Bearer "):
                token = supplied[7:]
        return self.store.session_user(token) if token else None

    def authorized(self, admin_only: bool = False) -> bool:
        user = self.session_user()
        if user:
            return user["role"] == "admin" or (not admin_only and user["role"] in {"operator", "viewer"})
        admin_token = getattr(self.server, "admin_token", "")  # type: ignore[attr-defined]
        read_token = getattr(self.server, "read_token", "")  # type: ignore[attr-defined]
        supplied = self.headers.get("Authorization", "")
        if admin_token and secrets.compare_digest(supplied, f"Bearer {admin_token}"):
            return True
        if not admin_only and read_token and secrets.compare_digest(supplied, f"Bearer {read_token}"):
            return True
        if self.store.user_count():
            return False
        return not admin_token and not read_token

    def require_auth(self) -> bool:
        if self.authorized(admin_only=True):
            return True
        self.send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def secure_connection(self) -> bool:
        return isinstance(self.connection, ssl.SSLSocket)

    def require_read_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def bootstrap_authorized(self) -> bool:
        expected = getattr(self.server, "bootstrap_token", "")  # type: ignore[attr-defined]
        supplied = self.headers.get("X-Bootstrap-Token", "")
        return bool(expected) and secrets.compare_digest(supplied, expected)

    def require_bootstrap(self) -> bool:
        if self.bootstrap_authorized():
            return True
        self.send_json({"error": "bootstrap authentication required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def device_authorized(self, device_id: str) -> bool:
        if self.authorized():
            return True
        supplied = self.headers.get("Authorization", "")
        return supplied.startswith("Bearer ") and self.store.device_token_valid(device_id, supplied[7:])

    def require_device_auth(self, device_id: str) -> bool:
        if self.device_authorized(device_id):
            return True
        self.send_json({"error": "device authentication required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/ca.crt":
            if not isinstance(self.connection, ssl.SSLSocket):
                self.send_json({"error": "download the local CA certificate over HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            ca_path = Path(getattr(self.server, "tls_ca_cert", ""))  # type: ignore[attr-defined]
            try:
                content = ca_path.read_bytes()
            except OSError:
                self.send_json({"error": "local CA certificate is not configured"}, HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/setup-ap"):
            device_id = unquote(parsed.path.split("/")[3])
            supplied = self.headers.get("Authorization", "")
            if not (supplied.startswith("Bearer ") and self.store.device_token_valid(device_id, supplied[7:])):
                self.send_json({"error": "device authentication required"}, HTTPStatus.UNAUTHORIZED)
                return
            if not isinstance(self.connection, ssl.SSLSocket):
                self.send_json({"error": "setup Wi-Fi settings require HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            device = self.store.device(device_id)
            if not device:
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            saved_profile = self.store.system_setting("setup_ap_profile") or {}
            enabled = bool(saved_profile.get("enabled", False))
            profile = {"enabled": enabled, "revision": saved_profile.get("revision", "disabled")}
            if enabled:
                profile.update({
                    "ssid": saved_profile.get("ssid", "PiCCTV-Setup"),
                    "password": saved_profile.get("password", ""),
                    "country": saved_profile.get("country", "US"),
                    "channel": saved_profile.get("channel", 6),
                })
            self.send_json(profile)
            return
        if parsed.path == "/api/server/network":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before changing server settings"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            saved = self.store.system_setting("public_stream_endpoints") or {}
            self.send_json({
                "media_url": saved.get("media_url", getattr(self.server, "public_media_url", "http://127.0.0.1:8888")),  # type: ignore[attr-defined]
                "webrtc_url": saved.get("webrtc_url", getattr(self.server, "public_webrtc_url", "http://127.0.0.1:8889")),  # type: ignore[attr-defined]
            })
            return
        if parsed.path == "/api/server/setup-ap":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before changing server settings"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            profile = self.store.system_setting("setup_ap_profile") or {}
            self.send_json({
                "enabled": bool(profile.get("enabled", False)),
                "ssid": profile.get("ssid", "PiCCTV-Setup"),
                "country": profile.get("country", "US"),
                "channel": profile.get("channel", 6),
                "password_configured": bool(profile.get("password")),
                "revision": profile.get("revision", "disabled"),
            })
            return
        if parsed.path == "/api/server/update":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before using server updates"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            host_update_dir = Path(getattr(self.server, "host_update_dir", "/host-update"))  # type: ignore[attr-defined]
            if not (host_update_dir / "installed").is_file():
                self.send_json({"installed": False, "state": "not_installed"})
                return
            try:
                status = json.loads((host_update_dir / "status.json").read_text())
            except (OSError, json.JSONDecodeError):
                status = {"state": "idle"}
            self.send_json({"installed": True, **status})
            return
        if parsed.path == "/api/server/os-update":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before using server updates"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            host_update_dir = Path(getattr(self.server, "host_update_dir", "/host-update"))  # type: ignore[attr-defined]
            if not (host_update_dir / "os-installed").is_file():
                self.send_json({"installed": False, "state": "not_installed"})
                return
            try:
                status = json.loads((host_update_dir / "os-status.json").read_text())
            except (OSError, json.JSONDecodeError):
                status = {"state": "idle"}
            self.send_json({"installed": True, **status})
            return
        if parsed.path == "/api/server/reboot":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before rebooting the server"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            host_update_dir = Path(getattr(self.server, "host_update_dir", "/host-update"))  # type: ignore[attr-defined]
            if not (host_update_dir / "reboot-installed").is_file():
                self.send_json({"installed": False, "state": "not_installed"})
                return
            try:
                status = json.loads((host_update_dir / "reboot-status.json").read_text())
            except (OSError, json.JSONDecodeError):
                status = {"state": "idle"}
            self.send_json({"installed": True, **status})
            return
        if parsed.path == "/api/auth/status":
            user = self.session_user()
            self.send_json({"authenticated": bool(user), "setup_required": self.store.user_count() == 0, "user": user})
            return
        if parsed.path == "/api/users":
            if not self.require_auth():
                return
            self.send_json({"users": self.store.users()})
            return
        if parsed.path == "/":
            content = (ROOT / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "version": __version__, "latest_agent_version": __agent_version__, "devices": self.store.health_summary()})
            return
        if parsed.path == "/api/fleet/os-update":
            if not self.require_auth():
                return
            self.send_json({"job": self.server.fleet_os_updater.status()})  # type: ignore[attr-defined]
            return
        if parsed.path == "/api/fleet/agent-update":
            if not self.require_auth():
                return
            self.send_json({"job": self.server.fleet_agent_updater.status()})  # type: ignore[attr-defined]
            return
        if parsed.path == "/api/server/health":
            if not self.require_read_auth():
                return
            memory = {}
            try:
                for line in Path("/proc/meminfo").read_text().splitlines():
                    key, _, value = line.partition(":")
                    if key in {"MemTotal", "MemAvailable"}:
                        memory[key.lower()] = int(value.strip().split()[0]) * 1024
            except (OSError, ValueError, IndexError):
                pass
            try:
                load_1m = os.getloadavg()[0]
            except (AttributeError, OSError):
                load_1m = None
            recordings_dir = Path(getattr(self.server, "recordings_dir", "recordings"))  # type: ignore[attr-defined]
            try:
                disk = shutil.disk_usage(recordings_dir)
                recordings_storage = {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free}
            except OSError:
                recordings_storage = None
            db_path = Path(self.store.path)
            try:
                database_bytes = sum(Path(str(db_path) + suffix).stat().st_size for suffix in ("", "-wal"))
            except OSError:
                database_bytes = 0
            started = getattr(self.server, "started_monotonic", time.monotonic())  # type: ignore[attr-defined]
            self.send_json({
                "version": __version__,
                "process_uptime_seconds": max(0, int(time.monotonic() - started)),
                "load_1m": load_1m,
                "memory_total_bytes": memory.get("memtotal"),
                "memory_available_bytes": memory.get("memavailable"),
                "recordings_storage": recordings_storage,
                "database_bytes": database_bytes,
            })
            return
        if parsed.path == "/api/nvr/settings":
            if not self.require_read_auth(): return
            path = Path(getattr(self.server, "recordings_dir", "recordings")) / "nvr-settings.json"  # type: ignore[attr-defined]
            try: settings = {**DEFAULT_NVR_SETTINGS, **json.loads(path.read_text())}
            except (OSError, ValueError, TypeError): settings = dict(DEFAULT_NVR_SETTINGS)
            self.send_json({"settings": settings})
            return
        if parsed.path == "/api/devices":
            if not self.require_read_auth():
                return
            self.send_json({"devices": self.store.devices(parse_qs(parsed.query).get("kind", [None])[0])})
            return
        if parsed.path == "/api/pending":
            if not self.require_auth():
                return
            self.send_json({"devices": self.store.pending_devices()})
            return
        if parsed.path.startswith("/api/enroll/"):
            if not isinstance(self.connection, ssl.SSLSocket):
                self.send_json({"error": "device enrollment requires HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            if not self.require_bootstrap():
                return
            pending_id = parsed.path.split("/")[3]
            claim = self.store.claim_adoption(pending_id)
            if claim:
                claim["device_token"] = claim.pop("issued_token")
                claim.pop("token_hash", None)
                claim["status"] = "adopted"
            self.send_json(claim or {"status": "pending"})
            return
        if parsed.path.startswith("/api/cameras/") and parsed.path.endswith("/live"):
            camera_id = parsed.path.split("/")[3]
            if not self.require_read_auth():
                return
            camera = self.store.device(camera_id)
            if not camera or camera["kind"] != "camera":
                self.send_json({"error": "camera not found"}, HTTPStatus.NOT_FOUND)
                return
            media_url = getattr(self.server, "public_media_url", "http://127.0.0.1:8888").rstrip("/")  # type: ignore[attr-defined]
            webrtc_url = getattr(self.server, "public_webrtc_url", "http://127.0.0.1:8889").rstrip("/")  # type: ignore[attr-defined]
            configured_endpoints = self.store.system_setting("public_stream_endpoints") or {}
            media_url = str(configured_endpoints.get("media_url", media_url)).rstrip("/")
            webrtc_url = str(configured_endpoints.get("webrtc_url", webrtc_url)).rstrip("/")
            self.send_json({"camera_id": camera_id, "hls_url": f"{media_url}/{camera_id}/index.m3u8", "webrtc_url": f"{webrtc_url}/{camera_id}/whep"})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/commands"):
            device_id = parsed.path.split("/")[3]
            if not self.require_device_auth(device_id):
                return
            if not self.store.device(device_id):
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"commands": self.store.consume_commands(device_id)})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/command-history"):
            if not self.require_auth():
                return
            device_id = parsed.path.split("/")[3]
            if not self.store.device(device_id):
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"commands": self.store.command_history(device_id)})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/diagnostics"):
            if not self.require_auth(): return
            device_id = parsed.path.split("/")[3]
            device = self.store.device(device_id)
            if not device:
                self.send_json({"error":"device not found"}, HTTPStatus.NOT_FOUND); return
            try: report = json.loads(device.get("metadata", "{}")).get("diagnostics")
            except (ValueError, TypeError): report = None
            self.send_json({"diagnostics":report})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/logs"):
            if not self.require_read_auth(): return
            device_id = parsed.path.split("/")[3]
            device = self.store.device(device_id)
            if not device:
                self.send_json({"error":"device not found"}, HTTPStatus.NOT_FOUND); return
            try: logs = json.loads(device.get("metadata", "{}")).get("recent_logs", [])
            except (ValueError, TypeError): logs = []
            self.send_json({"logs": logs[-200:]})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/maintenance/os-update"):
            if not self.require_auth():
                return
            device_id = unquote(parsed.path.split("/")[3])
            if not self.store.device(device_id):
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"maintenance": self.store.maintenance_run(device_id, "os_update")})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/maintenance/setup-ap-sync"):
            if not self.require_auth():
                return
            device_id = unquote(parsed.path.split("/")[3])
            if not self.store.device(device_id):
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"maintenance": self.store.maintenance_run(device_id, "setup_ap_sync")})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/preview"):
            if not self.require_read_auth():
                return
            device_id = unquote(parsed.path.split("/")[3])
            device = self.store.device(device_id)
            if not device or device.get("kind") != "monitor":
                self.send_json({"error": "monitor not found"}, HTTPStatus.NOT_FOUND)
                return
            filename = re.sub(r"[^A-Za-z0-9_.-]", "_", device_id) + ".jpg"
            image_path = Path(getattr(self.server, "previews_dir", "monitor-previews")) / filename  # type: ignore[attr-defined]
            try:
                image = image_path.read_bytes()
            except OSError:
                self.send_json({"error": "monitor preview is not available yet"}, HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
            return
        if parsed.path.startswith("/api/devices/"):
            device_id = parsed.path.split("/")[3]
            if not self.require_device_auth(device_id):
                return
            device = self.store.device(device_id)
            self.send_json(device or {"error": "device not found"}, HTTPStatus.OK if device else HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/assignments":
            if not self.require_read_auth():
                return
            self.send_json({"assignments": self.store.all_assignments()})
            return
        if parsed.path == "/api/audit":
            if not self.require_auth():
                return
            try:
                limit = int(parse_qs(parsed.query).get("limit", [100])[0])
            except ValueError:
                limit = 100
            self.send_json({"entries": self.store.audit_entries(limit)})
            return
        if parsed.path == "/api/recordings":
            if not self.require_read_auth():
                return
            root = Path(getattr(self.server, "recordings_dir", "recordings"))  # type: ignore[attr-defined]
            camera_id = parse_qs(parsed.query).get("camera_id", [None])[0]
            entries = []
            recordings = (*root.glob("**/*.mkv"), *root.glob("**/*.mp4"))
            for recording in recordings:
                relative = recording.relative_to(root)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if camera_id and relative.parts[0] != camera_id:
                    continue
                stat = recording.stat()
                modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                if datetime.now(timezone.utc) - modified_at < timedelta(seconds=10):
                    continue
                start = None
                try:
                    start = datetime.strptime(f"{relative.parts[-2]} {recording.stem}", "%Y-%m-%d %H-%M-%S").replace(tzinfo=timezone.utc)
                except (IndexError, ValueError):
                    pass
                entries.append({"path": str(relative), "camera_id": relative.parts[0] if relative.parts else "", "bytes": stat.st_size, "modified": modified_at.isoformat(), "start": start.isoformat() if start else None, "url": "/recordings/" + str(relative)})
            entries.sort(key=lambda entry: entry["modified"], reverse=True)
            self.send_json({"recordings": entries})
            return
        if parsed.path.startswith("/recordings/"):
            if not self.require_read_auth():
                return
            root = Path(getattr(self.server, "recordings_dir", "recordings")).resolve()
            requested = (root / unquote(parsed.path.removeprefix("/recordings/"))).resolve()
            if root not in requested.parents or not requested.is_file() or requested.suffix not in {".mkv", ".mp4"}:
                self.send_json({"error": "recording not found"}, HTTPStatus.NOT_FOUND)
                return
            if requested.suffix == ".mkv" and parse_qs(parsed.query).get("format", [""])[0] == "mp4":
                if not shutil.which("ffmpeg"):
                    self.send_json({"error": "MP4 playback conversion is unavailable"}, HTTPStatus.NOT_IMPLEMENTED)
                    return
                relative = requested.relative_to(root)
                cache = root / ".playback-cache" / relative.parent / f"{relative.stem}.mp4"
                cache.parent.mkdir(parents=True, exist_ok=True)
                with PLAYBACK_CACHE_LOCK:
                    if not cache.is_file() or cache.stat().st_mtime < requested.stat().st_mtime:
                        temporary = cache.with_name(f"{cache.stem}.{os.getpid()}.tmp.mp4")
                        try:
                            result = subprocess.run(
                                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(requested), "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", str(temporary)],
                                capture_output=True,
                                text=True,
                                timeout=180,
                            )
                        except (OSError, subprocess.TimeoutExpired):
                            temporary.unlink(missing_ok=True)
                            self.send_json({"error": "Unable to prepare this recording for browser playback"}, HTTPStatus.UNPROCESSABLE_ENTITY)
                            return
                        if result.returncode != 0 or not temporary.is_file():
                            temporary.unlink(missing_ok=True)
                            self.send_json({"error": "Unable to prepare this recording for browser playback"}, HTTPStatus.UNPROCESSABLE_ENTITY)
                            return
                        temporary.replace(cache)
                requested = cache
            file_size = requested.stat().st_size
            byte_range = self.headers.get("Range", "")
            start, end = 0, file_size - 1
            status = HTTPStatus.OK
            if byte_range.startswith("bytes="):
                try:
                    first, last = byte_range[6:].split("-", 1)
                    if first:
                        start = int(first)
                        end = min(int(last), file_size - 1) if last else file_size - 1
                    else:
                        length = int(last)
                        start = max(0, file_size - length)
                    if start > end or start >= file_size:
                        raise ValueError("range outside file")
                    status = HTTPStatus.PARTIAL_CONTENT
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(status)
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Type", "video/mp4" if requested.suffix == ".mp4" else "video/x-matroska")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with requested.open("rb") as recording_file:
                recording_file.seek(start)
                remaining = length
                while remaining:
                    chunk = recording_file.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        if parsed.path.startswith("/api/monitors/") and parsed.path.endswith("/assignment"):
            monitor_id = parsed.path.split("/")[3]
            if not self.require_device_auth(monitor_id):
                return
            assignment = self.store.assignment(monitor_id) or {"monitor_id": monitor_id, "layout": "single", "cameras": "[]", "outputs": "[]"}
            monitor = self.store.device(monitor_id)
            try:
                monitor_settings = json.loads(monitor.get("metadata", "{}")).get("settings", {}) if monitor else {}
            except (TypeError, json.JSONDecodeError):
                monitor_settings = {}
            cameras = {device["id"]: device for device in self.store.devices("camera")}
            assigned_ids = set()
            try:
                assigned_ids.update(json.loads(assignment.get("cameras", "[]")))
                for output in json.loads(assignment.get("outputs", "[]")):
                    assigned_ids.update(output.get("cameras", []))
            except (AttributeError, TypeError, json.JSONDecodeError):
                pass
            fallback_id = monitor_settings.get("fallback_camera", "")
            fallback = cameras.get(fallback_id)
            assignment["offline_cameras"] = sorted(
                camera_id for camera_id in assigned_ids
                if camera_id in cameras and cameras[camera_id].get("status") != "online"
            )
            assignment["fallback_camera"] = fallback_id if fallback and fallback.get("status") == "online" else ""
            self.send_json(assignment)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/fleet/os-update":
                if not self.require_auth():
                    return
                try:
                    job, started = self.server.fleet_os_updater.start()  # type: ignore[attr-defined]
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                    return
                self.store.audit("fleet.os_update.request", job["id"], self.client_address[0], {
                    "targets": [target["device_id"] for target in job["targets"]],
                })
                self.send_json({"job": job, "started": started}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/fleet/agent-update":
                if not self.require_auth():
                    return
                try:
                    job, started = self.server.fleet_agent_updater.start()  # type: ignore[attr-defined]
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                    return
                self.store.audit("fleet.agent_update.request", job["id"], self.client_address[0], {
                    "targets": [target["device_id"] for target in job["targets"]],
                })
                self.send_json({"job": job, "started": started}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/server/update":
                if not self.store.user_count():
                    self.send_json({"error": "create an administrator account before using server updates"}, HTTPStatus.UNAUTHORIZED)
                    return
                if not self.require_auth():
                    return
                if not self.secure_connection():
                    self.send_json({"error": "server updates can only be requested over HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                    return
                host_update_dir = Path(getattr(self.server, "host_update_dir", "/host-update"))  # type: ignore[attr-defined]
                if not (host_update_dir / "installed").is_file():
                    self.send_json({"error": "The host update helper is not installed"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                for status_name, request_name in (("status.json", "request.json"), ("os-status.json", "os-request.json"), ("reboot-status.json", "reboot-request.json")):
                    try:
                        current = json.loads((host_update_dir / status_name).read_text())
                    except (OSError, json.JSONDecodeError):
                        current = {}
                    if current.get("state") in {"queued", "running", "rebooting"} or (host_update_dir / request_name).exists():
                        self.send_json({"error": "Another server maintenance action is already in progress", "state": current.get("state", "queued")}, HTTPStatus.CONFLICT)
                        return
                host_update_dir.mkdir(parents=True, exist_ok=True)
                request_id = secrets.token_hex(16)
                request_path = host_update_dir / "request.json"
                if request_path.exists():
                    self.send_json({"error": "A server update request is already queued"}, HTTPStatus.CONFLICT)
                    return
                descriptor, temporary = tempfile.mkstemp(prefix=".update-request-", dir=host_update_dir, text=True)
                try:
                    os.fchown(descriptor, -1, host_update_dir.stat().st_gid)
                    os.fchmod(descriptor, 0o640)
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump({"id": request_id, "action": "update"}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    status = {"request_id": request_id, "state": "queued", "requested_at": datetime.now(timezone.utc).isoformat()}
                    (host_update_dir / "status.json").write_text(json.dumps(status) + "\n")
                    os.replace(temporary, request_path)
                except OSError:
                    try:
                        (host_update_dir / "status.json").write_text(json.dumps({"state": "idle"}) + "\n")
                    except OSError:
                        pass
                    self.send_json({"error": "The host update request could not be written"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                self.store.audit("server.update.request", "server", self.client_address[0], {"request_id": request_id})
                self.send_json({"ok": True, **status}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/server/os-update":
                if not self.store.user_count():
                    self.send_json({"error": "create an administrator account before using server updates"}, HTTPStatus.UNAUTHORIZED)
                    return
                if not self.require_auth():
                    return
                if not self.secure_connection():
                    self.send_json({"error": "server OS updates can only be requested over HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                    return
                host_update_dir = Path(getattr(self.server, "host_update_dir", "/host-update"))  # type: ignore[attr-defined]
                if not (host_update_dir / "os-installed").is_file():
                    self.send_json({"error": "The host OS update helper is not installed"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                for status_name, request_name in (("status.json", "request.json"), ("os-status.json", "os-request.json"), ("reboot-status.json", "reboot-request.json")):
                    try:
                        current = json.loads((host_update_dir / status_name).read_text())
                    except (OSError, json.JSONDecodeError):
                        current = {}
                    if current.get("state") in {"queued", "running", "rebooting"} or (host_update_dir / request_name).exists():
                        self.send_json({"error": "Another server maintenance action is already in progress", "state": current.get("state", "queued")}, HTTPStatus.CONFLICT)
                        return
                request_id = secrets.token_hex(16)
                request_path = host_update_dir / "os-request.json"
                descriptor, temporary = tempfile.mkstemp(prefix=".os-update-request-", dir=host_update_dir, text=True)
                try:
                    os.fchown(descriptor, -1, host_update_dir.stat().st_gid)
                    os.fchmod(descriptor, 0o640)
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump({"id": request_id, "action": "os_update"}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    status = {"request_id": request_id, "state": "queued", "requested_at": datetime.now(timezone.utc).isoformat()}
                    (host_update_dir / "os-status.json").write_text(json.dumps(status) + "\n")
                    os.replace(temporary, request_path)
                except OSError:
                    self.send_json({"error": "The host OS update request could not be written"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                self.store.audit("server.os_update.request", "server", self.client_address[0], {"request_id": request_id})
                self.send_json({"ok": True, **status}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/server/reboot":
                if not self.store.user_count():
                    self.send_json({"error": "create an administrator account before rebooting the server"}, HTTPStatus.UNAUTHORIZED)
                    return
                if not self.require_auth():
                    return
                if not self.secure_connection():
                    self.send_json({"error": "server reboots can only be requested over HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                    return
                host_update_dir = Path(getattr(self.server, "host_update_dir", "/host-update"))  # type: ignore[attr-defined]
                if not (host_update_dir / "reboot-installed").is_file():
                    self.send_json({"error": "The host reboot helper is not installed"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                for status_name, request_name in (("status.json", "request.json"), ("os-status.json", "os-request.json"), ("reboot-status.json", "reboot-request.json")):
                    try:
                        current = json.loads((host_update_dir / status_name).read_text())
                    except (OSError, json.JSONDecodeError):
                        current = {}
                    if current.get("state") in {"queued", "running", "rebooting"} or (host_update_dir / request_name).exists():
                        self.send_json({"error": "Another server maintenance action is already in progress", "state": current.get("state", "queued")}, HTTPStatus.CONFLICT)
                        return
                request_id = secrets.token_hex(16)
                request_path = host_update_dir / "reboot-request.json"
                descriptor, temporary = tempfile.mkstemp(prefix=".reboot-request-", dir=host_update_dir, text=True)
                try:
                    os.fchown(descriptor, -1, host_update_dir.stat().st_gid)
                    os.fchmod(descriptor, 0o640)
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump({"id": request_id, "action": "reboot"}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    status = {"request_id": request_id, "state": "queued", "requested_at": datetime.now(timezone.utc).isoformat()}
                    (host_update_dir / "reboot-status.json").write_text(json.dumps(status) + "\n")
                    os.replace(temporary, request_path)
                except OSError:
                    self.send_json({"error": "The host reboot request could not be written"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                self.store.audit("server.reboot.request", "server", self.client_address[0], {"request_id": request_id})
                self.send_json({"ok": True, **status}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/enroll" and not isinstance(self.connection, ssl.SSLSocket):
                self.send_json({"error": "device enrollment requires HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            if parsed.path == "/api/auth/setup":
                if self.store.user_count():
                    self.send_json({"error": "setup already completed"}, HTTPStatus.CONFLICT)
                    return
                payload = self.body()
                user = self.store.create_user(str(payload.get("username", "")), str(payload.get("password", "")), "admin")
                token = self.store.create_session(user["username"])
                self.send_response(HTTPStatus.CREATED)
                self.send_header("Set-Cookie", f"cctv_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"user": user, "session_token": token}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/auth/login":
                payload = self.body()
                user = self.store.authenticate_user(str(payload.get("username", "")), str(payload.get("password", "")))
                if not user:
                    self.send_json({"error": "invalid username or password"}, HTTPStatus.UNAUTHORIZED)
                    return
                token = self.store.create_session(user["username"])
                self.send_response(HTTPStatus.OK)
                self.send_header("Set-Cookie", f"cctv_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"user": user, "session_token": token}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/auth/logout":
                token = self.session_token()
                if not token:
                    supplied = self.headers.get("Authorization", "")
                    token = supplied[7:] if supplied.startswith("Bearer ") else ""
                if token:
                    self.store.revoke_session(token)
                self.send_response(HTTPStatus.OK)
                self.send_header("Set-Cookie", "cctv_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                self.send_header("Content-Type", "application/json")
                body = b'{"ok":true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/users":
                if not self.require_auth():
                    return
                payload = self.body()
                user = self.store.create_user(str(payload.get("username", "")), str(payload.get("password", "")), str(payload.get("role", "viewer")))
                self.store.audit("user.create", user["username"], self.client_address[0], {"role": user["role"]})
                self.send_json({"user": user}, HTTPStatus.CREATED)
                return
            if parsed.path == "/api/devices":
                if not self.require_auth():
                    return
                payload = self.body()
                missing = [field for field in ("id", "kind", "name") if not payload.get(field)]
                if missing or payload.get("kind") not in ("camera", "monitor"):
                    self.send_json({"error": "id, name, and kind (camera or monitor) are required"}, HTTPStatus.BAD_REQUEST)
                    return
                device = self.store.register(payload)
                self.store.audit("device.register", payload["id"], self.client_address[0], {"kind": payload["kind"], "name": payload["name"]})
                self.send_json(device, HTTPStatus.CREATED)
                return
            if parsed.path == "/api/enroll":
                if not self.require_bootstrap():
                    return
                payload = self.body()
                device_id = str(payload.get("id", "")).strip()
                if not device_id:
                    self.send_json({"error": "id is required"}, HTTPStatus.BAD_REQUEST)
                    return
                pending = self.store.pending_register(device_id, payload.get("name", device_id), self.client_address[0], payload.get("metadata", {}))
                self.send_json({"status": "pending", "id": pending["id"]}, HTTPStatus.ACCEPTED)
                return
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/heartbeat"):
                device_id = parsed.path.split("/")[3]
                if not self.require_device_auth(device_id):
                    return
                device = self.store.heartbeat(device_id, self.client_address[0], json.dumps(self.body()))
                self.send_json(device or {"error": "device not found"}, HTTPStatus.OK if device else HTTPStatus.NOT_FOUND)
                return
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/preview"):
                device_id = unquote(parsed.path.split("/")[3])
                if not self.require_device_auth(device_id):
                    return
                device = self.store.device(device_id)
                if not device or device.get("kind") != "monitor":
                    self.send_json({"error": "monitor not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if self.headers.get_content_type() != "image/jpeg" or not 4 <= length <= 1_500_000:
                    self.send_json({"error": "a JPEG preview up to 1.5 MB is required"}, HTTPStatus.BAD_REQUEST)
                    return
                image = self.rfile.read(length)
                if len(image) != length or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
                    self.send_json({"error": "preview is not a complete JPEG image"}, HTTPStatus.BAD_REQUEST)
                    return
                filename = re.sub(r"[^A-Za-z0-9_.-]", "_", device_id) + ".jpg"
                root = Path(getattr(self.server, "previews_dir", "monitor-previews"))  # type: ignore[attr-defined]
                root.mkdir(parents=True, exist_ok=True)
                target = root / filename
                temporary = root / (filename + ".tmp")
                temporary.write_bytes(image)
                os.replace(temporary, target)
                self.send_json({"ok": True})
                return
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/diagnostics"):
                device_id = parsed.path.split("/")[3]
                if not self.require_device_auth(device_id): return
                device = self.store.device(device_id)
                if not device:
                    self.send_json({"error":"device not found"}, HTTPStatus.NOT_FOUND); return
                payload=self.body().get("diagnostics")
                if not isinstance(payload,dict):
                    self.send_json({"error":"diagnostics object is required"}, HTTPStatus.BAD_REQUEST); return
                self.store.heartbeat(device_id, self.client_address[0], json.dumps({"diagnostics":payload}))
                self.send_json({"ok":True})
                return
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/maintenance/os-update"):
                device_id = unquote(parsed.path.split("/")[3])
                if not self.require_device_auth(device_id):
                    return
                if not self.store.device(device_id):
                    self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                    return
                payload = self.body()
                state = payload.get("state")
                details = payload.get("details", {})
                if state not in {"running", "succeeded", "failed"} or not isinstance(details, dict):
                    self.send_json({"error": "valid state and details are required"}, HTTPStatus.BAD_REQUEST)
                    return
                safe_details = {
                    "output": str(details.get("output", ""))[-12000:],
                    "reboot_required": bool(details.get("reboot_required", False)),
                }
                result = self.store.set_maintenance_run(device_id, "os_update", state, safe_details)
                self.send_json({"ok": True, "maintenance": result})
                return
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/maintenance/setup-ap-sync"):
                device_id = unquote(parsed.path.split("/")[3])
                if not self.require_device_auth(device_id):
                    return
                if not self.store.device(device_id):
                    self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                    return
                payload = self.body()
                state = payload.get("state")
                details = payload.get("details", {})
                if state not in {"running", "succeeded", "failed"} or not isinstance(details, dict):
                    self.send_json({"error": "valid state and details are required"}, HTTPStatus.BAD_REQUEST)
                    return
                safe_details = {"message": str(details.get("message", ""))[-1000:]}
                result = self.store.set_maintenance_run(device_id, "setup_ap_sync", state, safe_details)
                self.send_json({"ok": True, "maintenance": result})
                return
            if parsed.path.startswith("/api/pending/") and parsed.path.endswith("/adopt"):
                if not self.require_auth():
                    return
                pending_id = parsed.path.split("/")[3]
                payload = self.body()
                if payload.get("kind") not in ("camera", "monitor") or not payload.get("name"):
                    self.send_json({"error": "kind and name are required"}, HTTPStatus.BAD_REQUEST)
                    return
                result = self.store.adopt_pending(pending_id, payload["kind"], payload["name"], payload.get("location", ""))
                if not result:
                    self.send_json({"error": "pending device not found or already adopted"}, HTTPStatus.NOT_FOUND)
                    return
                device, _token = result
                self.store.audit("device.adopt", pending_id, self.client_address[0], {"kind": payload["kind"], "name": payload["name"]})
                self.send_json(device, HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/commands"):
                if not self.require_auth():
                    return
                device_id = parsed.path.split("/")[3]
                if not self.store.device(device_id):
                    self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                    return
                payload = self.body()
                allowed_commands = {"restart_media", "diagnostics", "restart_agent", "reboot", "update", "update_os", "sync_setup_ap"}
                command_name = str(payload.get("command", ""))
                if command_name not in allowed_commands:
                    self.send_json({"error": "unsupported diagnostic command"}, HTTPStatus.BAD_REQUEST)
                    return
                if command_name == "update_os":
                    try:
                        current_agent_version = json.loads(self.store.device(device_id).get("metadata", "{}" )).get("agent_version", "")
                    except (ValueError, TypeError, AttributeError):
                        current_agent_version = ""
                    if current_agent_version != __agent_version__:
                        self.send_json({"error": f"Update CCTV software to {__agent_version__} first, then retry the Pi OS update."}, HTTPStatus.CONFLICT)
                        return
                    maintenance = self.store.maintenance_run(device_id, "os_update")
                    if maintenance and maintenance["state"] in {"queued", "running"}:
                        self.send_json({"error": "a Pi OS update is already queued or running"}, HTTPStatus.CONFLICT)
                        return
                    self.store.set_maintenance_run(device_id, "os_update", "queued", {})
                if command_name == "sync_setup_ap":
                    maintenance = self.store.maintenance_run(device_id, "setup_ap_sync")
                    if maintenance and maintenance["state"] in {"queued", "running"}:
                        self.send_json({"error": "setup Wi-Fi synchronization is already queued or running"}, HTTPStatus.CONFLICT)
                        return
                    self.store.set_maintenance_run(device_id, "setup_ap_sync", "queued", {})
                command = self.store.queue_command(device_id, command_name)
                self.store.audit("device.command", device_id, self.client_address[0], {"command": command_name})
                self.send_json(command, HTTPStatus.ACCEPTED)
                return
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/password":
            current = self.session_user()
            if not current:
                self.send_json({"error": "sign in with a dashboard account to change its password"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.secure_connection():
                self.send_json({"error": "password changes require HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            payload = self.body()
            current_password = payload.get("current_password", "")
            new_password = payload.get("new_password", "")
            if not isinstance(current_password, str) or not isinstance(new_password, str) or not 8 <= len(new_password) <= 128:
                self.send_json({"error": "new password must be 8–128 characters"}, HTTPStatus.BAD_REQUEST)
                return
            if not self.store.authenticate_user(current["username"], current_password):
                self.send_json({"error": "current password is incorrect"}, HTTPStatus.UNAUTHORIZED)
                return
            token = self.session_token()
            if not token:
                supplied = self.headers.get("Authorization", "")
                token = supplied[7:] if supplied.startswith("Bearer ") else ""
            try:
                self.store.set_password(current["username"], new_password, preserve_session_token=token)
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.store.audit("user.password_change", current["username"], self.client_address[0])
            self.send_json({"ok": True, "username": current["username"]})
            return
        if parsed.path.startswith("/api/users/") and parsed.path.endswith("/password"):
            if not self.require_auth():
                return
            if not self.secure_connection():
                self.send_json({"error": "password resets require HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            username = unquote(parsed.path[len("/api/users/"):-len("/password")].strip("/"))
            current = self.session_user()
            if current and current["username"] == username:
                self.send_json({"error": "use Change my password to update your own account"}, HTTPStatus.BAD_REQUEST)
                return
            payload = self.body()
            password = payload.get("new_password", "")
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                self.send_json({"error": "new password must be 8–128 characters"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self.store.set_password(username, password)
            except ValueError as error:
                status = HTTPStatus.NOT_FOUND if str(error) == "user not found" else HTTPStatus.BAD_REQUEST
                self.send_json({"error": str(error)}, status)
                return
            self.store.audit("user.password_reset", username, self.client_address[0], {"sessions_revoked": True})
            self.send_json({"ok": True, "username": username, "sessions_revoked": True})
            return
        if parsed.path == "/api/server/network":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before changing server settings"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            if not self.secure_connection():
                self.send_json({"error": "stream endpoints can only be changed over HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            payload = self.body()
            normalized = {}
            for key in ("media_url", "webrtc_url"):
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip() or len(value) > 255:
                    self.send_json({"error": "enter both media and WebRTC base URLs"}, HTTPStatus.BAD_REQUEST)
                    return
                value = value.strip().rstrip("/")
                try:
                    endpoint = urlparse(value)
                    valid = (endpoint.scheme in {"http", "https"} and bool(endpoint.hostname)
                             and endpoint.username is None and endpoint.password is None
                             and not endpoint.query and not endpoint.fragment)
                    endpoint.port  # validate any explicit port
                except ValueError:
                    valid = False
                if not valid:
                    self.send_json({"error": f"{key} must be a valid HTTP or HTTPS URL without credentials, query, or fragment"}, HTTPStatus.BAD_REQUEST)
                    return
                normalized[key] = value
            self.store.set_system_setting("public_stream_endpoints", normalized)
            self.store.audit("server.stream_endpoints.update", "stream-endpoints", self.client_address[0], normalized)
            self.send_json(normalized)
            return
        if parsed.path == "/api/server/setup-ap":
            if not self.store.user_count():
                self.send_json({"error": "create an administrator account before changing server settings"}, HTTPStatus.UNAUTHORIZED)
                return
            if not self.require_auth():
                return
            if not isinstance(self.connection, ssl.SSLSocket):
                self.send_json({"error": "setup Wi-Fi settings can only be changed over HTTPS"}, HTTPStatus.UPGRADE_REQUIRED)
                return
            payload = self.body()
            old_profile = self.store.system_setting("setup_ap_profile") or {}
            enabled = payload.get("enabled")
            ssid = str(payload.get("ssid", "")).strip()
            password = payload.get("password", "")
            country = payload.get("country", "")
            channel = payload.get("channel")
            if not isinstance(enabled, bool):
                self.send_json({"error": "enabled must be true or false"}, HTTPStatus.BAD_REQUEST); return
            if not re.fullmatch(r"[\x20-\x7e]{1,32}", ssid):
                self.send_json({"error": "SSID must be 1–32 printable ASCII characters"}, HTTPStatus.BAD_REQUEST); return
            if password == "":
                password = old_profile.get("password", "")
            if not isinstance(password, str) or (enabled and not re.fullmatch(r"[\x20-\x7e]{8,63}", password)):
                self.send_json({"error": "Wi-Fi password must be 8–63 printable ASCII characters"}, HTTPStatus.BAD_REQUEST); return
            if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
                self.send_json({"error": "Country must be a two-letter uppercase code"}, HTTPStatus.BAD_REQUEST); return
            if isinstance(channel, bool) or not isinstance(channel, int) or channel not in range(1, 12):
                self.send_json({"error": "Wi-Fi channel must be 1–11"}, HTTPStatus.BAD_REQUEST); return
            if enabled and (not password or not getattr(self.server, "tls_enabled", False)):
                self.send_json({"error": "Enable server HTTPS and set a Wi-Fi password before enabling the setup AP"}, HTTPStatus.BAD_REQUEST); return
            try:
                next_revision = int(old_profile.get("revision", "0")) + 1
            except (TypeError, ValueError):
                next_revision = 1
            next_profile = {
                "enabled": enabled, "ssid": ssid, "password": password,
                "country": country, "channel": channel,
                "revision": str(next_revision),
            }
            self.store.set_system_setting("setup_ap_profile", next_profile)
            self.store.audit("server.setup_ap.update", "setup-ap", self.client_address[0], {
                "enabled": enabled, "ssid": ssid, "country": country, "channel": channel,
                "password_changed": payload.get("password", "") != "",
                "revision": next_profile["revision"],
            })
            self.send_json({
                "enabled": enabled, "ssid": ssid, "country": country, "channel": channel,
                "password_configured": bool(password), "revision": next_profile["revision"],
            })
            return
        if parsed.path == "/api/nvr/settings":
            if not self.require_auth(): return
            payload = self.body()
            try:
                enabled = payload.get("recording_enabled", True)
                retention = int(payload.get("retention_hours", 24))
                segment = int(payload.get("segment_seconds", 900))
                storage_limit = int(payload.get("storage_limit_gb", 500))
                camera_recording = payload.get("camera_recording", {})
            except (TypeError, ValueError):
                self.send_json({"error": "NVR settings contain invalid values"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(enabled, bool) or not (1 <= retention <= 8760 and 10 <= segment <= 3600 and 1 <= storage_limit <= 100000 and isinstance(camera_recording, dict)) or any(not isinstance(value, bool) for value in camera_recording.values()):
                self.send_json({"error": "Retention, segment length, storage cap, or camera selections are invalid"}, HTTPStatus.BAD_REQUEST)
                return
            camera_recording = {str(key): value for key, value in camera_recording.items() if self.store.device(str(key)) and self.store.device(str(key)).get("kind") == "camera"}
            saved = {"recording_enabled": enabled, "retention_hours": retention, "segment_seconds": segment, "storage_limit_gb": storage_limit, "camera_recording": camera_recording}
            path = Path(getattr(self.server, "recordings_dir", "recordings")) / "nvr-settings.json"  # type: ignore[attr-defined]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(saved, indent=2) + "\n")
            self.store.audit("nvr.settings.update", "nvr", self.client_address[0], saved)
            self.send_json({"settings": saved})
            return
        if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/profile"):
            if not self.require_auth():
                return
            device_id = unquote(parsed.path[len("/api/devices/"):-len("/profile")].strip("/"))
            device = self.store.device(device_id)
            if not device:
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            payload = self.body()
            name = payload.get("name")
            location = payload.get("location", "")
            name = name.strip() if isinstance(name, str) else ""
            location = location.strip() if isinstance(location, str) else None
            valid_name = 1 <= len(name) <= 80
            valid_location = isinstance(location, str) and len(location) <= 120
            if (not valid_name or not valid_location
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or any(ord(char) < 32 or ord(char) == 127 for char in location or "")):
                self.send_json({"error": "device name must be 1–80 characters and location must be 0–120 characters without control characters"}, HTTPStatus.BAD_REQUEST)
                return
            updated = self.store.update_profile(device_id, name, location)
            if not updated:
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            self.store.audit("device.profile.update", device_id, self.client_address[0], {"name": name, "location": location})
            self.send_json(updated)
            return
        if parsed.path.startswith("/api/cameras/") and parsed.path.endswith("/settings"):
            if not self.require_auth():
                return
            camera_id = parsed.path.split("/")[3]
            camera = self.store.device(camera_id)
            settings = self.body()
            allowed = {
                "width", "height", "fps", "bitrate", "exposure_mode", "shutter_us", "gain", "exposure_profile",
                "white_balance", "brightness", "contrast", "saturation", "sharpness",
                "flip_horizontal", "flip_vertical", "keyframe_interval", "live_profile",
            }
            if not camera or camera["kind"] != "camera":
                self.send_json({"error": "camera not found"}, HTTPStatus.NOT_FOUND)
            else:
                invalid = any(key not in allowed for key in settings)
                integer_ranges = {"width": (320, 7680), "height": (240, 4320), "fps": (1, 120), "bitrate": (100_000, 100_000_000), "shutter_us": (100, 2_000_000), "keyframe_interval": (1, 300)}
                for key, (minimum, maximum) in integer_ranges.items():
                    if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], int) or not minimum <= settings[key] <= maximum):
                        invalid = True
                for key in ("brightness", "contrast", "saturation", "sharpness"):
                    if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], (int, float)) or not -1.0 <= float(settings[key]) <= 2.0):
                        invalid = True
                if "gain" in settings and (
                    isinstance(settings["gain"], bool) or not isinstance(settings["gain"], (int, float))
                    or not 1.0 <= float(settings["gain"]) <= 16.0
                ):
                    invalid = True
                for key, values in {
                    "exposure_mode": {"auto", "manual"}, "white_balance": {"auto", "daylight", "cloudy", "tungsten", "fluorescent"},
                    "exposure_profile": {"normal", "sport", "long"}, "live_profile": {"baseline", "main", "high"},
                }.items():
                    if key in settings and settings[key] not in values:
                        invalid = True
                for key in ("flip_horizontal", "flip_vertical"):
                    if key in settings and not isinstance(settings[key], bool):
                        invalid = True
                if invalid:
                    self.send_json({"error": "invalid camera settings or value outside supported range"}, HTTPStatus.BAD_REQUEST)
                else:
                    device = self.store.update_settings(camera_id, settings)
                    self.store.audit("camera.settings", camera_id, self.client_address[0], settings)
                    self.send_json(device)
            return
        if parsed.path.startswith("/api/monitors/") and parsed.path.endswith("/settings"):
            if not self.require_auth():
                return
            monitor_id = parsed.path.split("/")[3]
            monitor = self.store.device(monitor_id)
            settings = self.body()
            allowed = {
                "scale_mode", "orientation", "width", "height", "refresh_hz", "auto_resolution", "kiosk",
                "prevent_blanking", "show_camera_name", "show_timestamp", "reconnect_seconds",
                "watchdog_seconds", "fallback_camera", "schedule_enabled",
            }
            camera_ids = {device["id"] for device in self.store.devices("camera")}
            valid_enums = (
                settings.get("scale_mode", "fit") in ("fit", "fill")
                and settings.get("orientation", 0) in (0, 90, 180, 270)
                and (not settings.get("fallback_camera") or settings.get("fallback_camera") in camera_ids)
            )
            valid_types = all(
                (key in {"width", "height", "refresh_hz", "reconnect_seconds", "watchdog_seconds"}
                 and isinstance(value, int) and not isinstance(value, bool) and value > 0)
                or (key == "orientation" and isinstance(value, int) and not isinstance(value, bool) and value in (0, 90, 180, 270))
                or (key in {"kiosk", "prevent_blanking", "show_camera_name", "show_timestamp", "schedule_enabled"} and isinstance(value, bool))
                or (key == "auto_resolution" and isinstance(value, bool))
                or (key in {"scale_mode", "fallback_camera"} and isinstance(value, str))
                for key, value in settings.items()
            )
            reconnect_seconds = settings.get("reconnect_seconds", 5)
            watchdog_seconds = settings.get("watchdog_seconds", 30)
            valid_ranges = (
                isinstance(reconnect_seconds, int) and not isinstance(reconnect_seconds, bool) and 1 <= reconnect_seconds <= 120
                and isinstance(watchdog_seconds, int) and not isinstance(watchdog_seconds, bool) and 5 <= watchdog_seconds <= 600
            )
            if not monitor or monitor["kind"] != "monitor":
                self.send_json({"error": "monitor not found"}, HTTPStatus.NOT_FOUND)
            elif not settings or any(key not in allowed for key in settings) or not valid_types or not valid_enums or not valid_ranges:
                self.send_json({"error": "invalid monitor settings"}, HTTPStatus.BAD_REQUEST)
            else:
                device = self.store.update_settings(monitor_id, settings)
                self.store.audit("monitor.settings", monitor_id, self.client_address[0], settings)
                self.send_json(device)
            return
        if parsed.path.startswith("/api/monitors/") and parsed.path.endswith("/assignment"):
            if not self.require_auth():
                return
            monitor_id = parsed.path.split("/")[3]
            payload = self.body()
            monitor = self.store.device(monitor_id)
            cameras = payload.get("cameras", [])
            outputs = payload.get("outputs", [])
            camera_ids = {device["id"] for device in self.store.devices("camera")}
            try:
                metadata = json.loads(monitor["metadata"] or "{}") if monitor else {}
            except json.JSONDecodeError:
                metadata = {}
            displays = metadata.get("displays", [])
            display_count = len(displays) if isinstance(displays, list) and displays else int(metadata.get("display_count", 1) or 1)
            valid_outputs = (
                isinstance(outputs, list) and len(outputs) == display_count == 2
                and all(isinstance(output, dict) and output.get("layout") == "single" and isinstance(output.get("cameras", []), list)
                        and len(output.get("cameras", [])) <= 1 and all(camera in camera_ids for camera in output.get("cameras", []))
                        for output in outputs)
            )
            if not monitor or monitor["kind"] != "monitor":
                self.send_json({"error": "monitor not found"}, HTTPStatus.NOT_FOUND)
            elif payload.get("layout") not in ("single", "2x2") or not isinstance(cameras, list):
                self.send_json({"error": "layout must be single or 2x2 and cameras must be a list"}, HTTPStatus.BAD_REQUEST)
            elif display_count == 2 and not valid_outputs:
                self.send_json({"error": "a two-display monitor requires two single-camera outputs"}, HTTPStatus.BAD_REQUEST)
            elif display_count != 2 and outputs:
                self.send_json({"error": "outputs are only used for two-display monitors"}, HTTPStatus.BAD_REQUEST)
            elif len(cameras) > (2 if valid_outputs else (1 if payload["layout"] == "single" else 4)) or any(camera not in camera_ids for camera in cameras):
                self.send_json({"error": "invalid camera assignment"}, HTTPStatus.BAD_REQUEST)
            else:
                assignment = self.store.set_assignment(monitor_id, payload["layout"], cameras, outputs)
                self.store.audit("monitor.assignment", monitor_id, self.client_address[0], {"layout": payload["layout"], "cameras": cameras, "outputs": outputs})
                self.send_json(assignment)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/users/"):
            if not self.require_auth():
                return
            username = unquote(parsed.path.split("/", 3)[3])
            current = self.session_user()
            if current and current["username"] == username:
                self.send_json({"error": "cannot delete the current user"}, HTTPStatus.BAD_REQUEST)
                return
            if not any(user["username"] == username for user in self.store.users()):
                self.send_json({"error": "user not found"}, HTTPStatus.NOT_FOUND)
                return
            self.store.delete_user(username)
            self.store.audit("user.delete", username, self.client_address[0])
            self.send_json({"deleted": username})
            return
        if parsed.path.startswith("/api/devices/"):
            if not self.require_auth():
                return
            device_id = parsed.path.split("/")[3]
            if not self.store.device(device_id):
                self.send_json({"error": "device not found"}, HTTPStatus.NOT_FOUND)
                return
            self.store.delete_device(device_id)
            self.store.audit("device.delete", device_id, self.client_address[0])
            self.send_json({"deleted": device_id})
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi CCTV V1 management server")
    parser.add_argument("--host", default=os.getenv("CCTV_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CCTV_PORT", "8080")))
    parser.add_argument("--db", default=os.getenv("CCTV_DB", "data/cctv.db"))
    parser.add_argument("--admin-token", default=os.getenv("CCTV_ADMIN_TOKEN", ""))
    parser.add_argument("--read-token", default=os.getenv("CCTV_READ_TOKEN", ""))
    parser.add_argument("--recordings-dir", default=os.getenv("CCTV_RECORDINGS_DIR", "recordings"))
    parser.add_argument("--previews-dir", default=os.getenv("CCTV_PREVIEWS_DIR", ""))
    parser.add_argument("--bootstrap-token", default=os.getenv("CCTV_BOOTSTRAP_TOKEN", ""))
    parser.add_argument("--public-media-url", default=os.getenv("CCTV_PUBLIC_MEDIA_URL", "http://127.0.0.1:8888"))
    parser.add_argument("--public-webrtc-url", default=os.getenv("CCTV_PUBLIC_WEBRTC_URL", "http://127.0.0.1:8889"))
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.started_monotonic = time.monotonic()  # type: ignore[attr-defined]
    httpd.store = Store(args.db)  # type: ignore[attr-defined]
    fleet_update_lock = threading.Lock()
    httpd.fleet_agent_updater = FleetAgentUpdateCoordinator(httpd.store, __agent_version__, execution_lock=fleet_update_lock)  # type: ignore[attr-defined]
    httpd.fleet_os_updater = FleetOsUpdateCoordinator(httpd.store, __agent_version__, execution_lock=fleet_update_lock)  # type: ignore[attr-defined]
    httpd.admin_token = args.admin_token  # type: ignore[attr-defined]
    httpd.read_token = args.read_token  # type: ignore[attr-defined]
    httpd.recordings_dir = args.recordings_dir  # type: ignore[attr-defined]
    httpd.previews_dir = args.previews_dir or str(Path(args.db).parent / "monitor-previews")  # type: ignore[attr-defined]
    httpd.bootstrap_token = args.bootstrap_token  # type: ignore[attr-defined]
    httpd.public_media_url = args.public_media_url  # type: ignore[attr-defined]
    httpd.public_webrtc_url = args.public_webrtc_url  # type: ignore[attr-defined]
    httpd.tls_enabled = bool(os.getenv("CCTV_TLS_CERT") and os.getenv("CCTV_TLS_KEY"))  # type: ignore[attr-defined]
    httpd.host_update_dir = os.getenv("CCTV_HOST_UPDATE_DIR", "/host-update")  # type: ignore[attr-defined]
    ap_profile = httpd.store.system_setting("setup_ap_profile")  # type: ignore[attr-defined]
    if ap_profile is None:
        initial_ap_profile = {
            "enabled": os.getenv("CCTV_SETUP_AP_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            "ssid": os.getenv("CCTV_SETUP_AP_SSID", "PiCCTV-Setup"),
            "password": os.getenv("CCTV_SETUP_AP_PASSWORD", ""),
            "country": os.getenv("CCTV_SETUP_AP_COUNTRY", "US"),
            "channel": int(os.getenv("CCTV_SETUP_AP_CHANNEL", "6")),
            "revision": os.getenv("CCTV_SETUP_AP_REVISION", "1"),
        }
        httpd.store.set_system_setting("setup_ap_profile", initial_ap_profile, only_if_missing=True)  # type: ignore[attr-defined]
        ap_profile = httpd.store.system_setting("setup_ap_profile")  # type: ignore[attr-defined]
    if not isinstance(ap_profile, dict):
        raise SystemExit("Saved setup AP settings are invalid")
    if ap_profile.get("enabled") and not re.fullmatch(r"[\x20-\x7e]{8,63}", str(ap_profile.get("password", ""))):
        raise SystemExit("Saved setup AP password must be 8–63 printable ASCII characters")
    if not re.fullmatch(r"[\x20-\x7e]{1,32}", str(ap_profile.get("ssid", ""))):
        raise SystemExit("Saved setup AP SSID must be 1–32 printable ASCII characters")
    if not re.fullmatch(r"[A-Z]{2}", str(ap_profile.get("country", ""))):
        raise SystemExit("Saved setup AP country must be a two-letter uppercase code")
    if isinstance(ap_profile.get("channel"), bool) or ap_profile.get("channel") not in range(1, 12):
        raise SystemExit("Saved setup AP channel must be a 2.4 GHz channel from 1 to 11")
    if ap_profile.get("enabled") and not httpd.tls_enabled:  # type: ignore[attr-defined]
        raise SystemExit("HTTPS must be configured before enabling the server-managed setup Wi-Fi profile")
    setup_ap_enabled = bool(ap_profile.get("enabled"))
    httpd.recorder_supervisor = RecorderSupervisor(httpd.store, Path(args.recordings_dir))  # type: ignore[attr-defined]
    httpd.recorder_supervisor.start()  # type: ignore[attr-defined]
    print(f"Pi CCTV server listening on http://{args.host}:{args.port}", flush=True)
    tls_httpd = None
    tls_thread = None
    tls_cert = os.getenv("CCTV_TLS_CERT", "")
    tls_key = os.getenv("CCTV_TLS_KEY", "")
    httpd.tls_ca_cert = os.getenv("CCTV_TLS_CA_CERT", "")  # type: ignore[attr-defined]
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            raise SystemExit("Set both CCTV_TLS_CERT and CCTV_TLS_KEY to enable HTTPS")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls_cert, tls_key)
        tls_port = int(os.getenv("CCTV_TLS_PORT", "8443"))
        tls_httpd = ThreadingHTTPServer((args.host, tls_port), Handler)
        for key in (
            "store", "admin_token", "read_token", "recordings_dir", "previews_dir",
            "bootstrap_token", "public_media_url", "public_webrtc_url", "recorder_supervisor",
            "setup_ap_enabled", "tls_enabled",
            "tls_ca_cert", "host_update_dir", "started_monotonic",
            "fleet_os_updater",
            "fleet_agent_updater",
        ):
            setattr(tls_httpd, key, getattr(httpd, key))
        tls_httpd.socket = context.wrap_socket(tls_httpd.socket, server_side=True)
        tls_thread = threading.Thread(target=tls_httpd.serve_forever, name="https-api", daemon=True)
        tls_thread.start()
        print(f"Pi CCTV device API listening on https://{args.host}:{tls_port}", flush=True)
    elif setup_ap_enabled:
        raise SystemExit("HTTPS must be configured before enabling the server-managed setup Wi-Fi profile")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.fleet_agent_updater.stop()  # type: ignore[attr-defined]
        httpd.fleet_os_updater.stop()  # type: ignore[attr-defined]
        httpd.recorder_supervisor.stop()  # type: ignore[attr-defined]
        if tls_httpd is not None:
            tls_httpd.shutdown()
            tls_httpd.server_close()
        httpd.store.close()  # type: ignore[attr-defined]
        httpd.server_close()


if __name__ == "__main__":
    main()
