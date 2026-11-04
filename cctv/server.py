"""Dependency-free V1 management API and browser dashboard."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .store import Store


ROOT = Path(__file__).parent


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
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "version": "0.1.0", "devices": self.store.health_summary()})
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
            for recording in root.glob("**/*.mkv"):
                relative = recording.relative_to(root)
                if camera_id and relative.parts[0] != camera_id:
                    continue
                stat = recording.stat()
                entries.append({"path": str(relative), "bytes": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
            entries.sort(key=lambda entry: entry["modified"], reverse=True)
            self.send_json({"recordings": entries})
            return
        if parsed.path.startswith("/recordings/"):
            root = Path(getattr(self.server, "recordings_dir", "recordings")).resolve()
            requested = (root / unquote(parsed.path.removeprefix("/recordings/"))).resolve()
            if root not in requested.parents or not requested.is_file() or requested.suffix != ".mkv":
                self.send_json({"error": "recording not found"}, HTTPStatus.NOT_FOUND)
                return
            body = requested.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/api/monitors/") and parsed.path.endswith("/assignment"):
            monitor_id = parsed.path.split("/")[3]
            if not self.require_device_auth(monitor_id):
                return
            assignment = self.store.assignment(monitor_id)
            self.send_json(assignment or {"monitor_id": monitor_id, "layout": "single", "cameras": "[]", "outputs": "[]"})
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
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
                if payload.get("command") != "restart_media":
                    self.send_json({"error": "only restart_media is supported in V1"}, HTTPStatus.BAD_REQUEST)
                    return
                command = self.store.queue_command(device_id, payload["command"])
                self.store.audit("device.command", device_id, self.client_address[0], {"command": payload["command"]})
                self.send_json(command, HTTPStatus.ACCEPTED)
                return
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/cameras/") and parsed.path.endswith("/settings"):
            if not self.require_auth():
                return
            camera_id = parsed.path.split("/")[3]
            camera = self.store.device(camera_id)
            settings = self.body()
            allowed = {
                "width", "height", "fps", "bitrate", "exposure_mode", "shutter_us", "iso",
                "white_balance", "brightness", "contrast", "saturation", "sharpness",
                "flip_horizontal", "flip_vertical", "low_light_mode", "keyframe_interval",
                "bitrate_mode", "live_profile", "recording_profile",
            }
            if not camera or camera["kind"] != "camera":
                self.send_json({"error": "camera not found"}, HTTPStatus.NOT_FOUND)
            else:
                invalid = any(key not in allowed for key in settings)
                integer_ranges = {"width": (320, 7680), "height": (240, 4320), "fps": (1, 120), "bitrate": (100_000, 100_000_000), "shutter_us": (100, 2_000_000), "iso": (0, 3200), "keyframe_interval": (1, 300)}
                for key, (minimum, maximum) in integer_ranges.items():
                    if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], int) or not minimum <= settings[key] <= maximum):
                        invalid = True
                for key in ("brightness", "contrast", "saturation", "sharpness"):
                    if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], (int, float)) or not -1.0 <= float(settings[key]) <= 2.0):
                        invalid = True
                for key, values in {
                    "exposure_mode": {"auto", "manual"}, "white_balance": {"auto", "daylight", "cloudy", "tungsten", "fluorescent"},
                    "low_light_mode": {"off", "on"}, "bitrate_mode": {"cbr", "vbr"}, "live_profile": {"baseline", "main", "high"}, "recording_profile": {"baseline", "main", "high"},
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
                "scale_mode", "orientation", "width", "height", "refresh_hz", "kiosk",
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
                (key in {"width", "height", "refresh_hz", "orientation", "reconnect_seconds", "watchdog_seconds"}
                 and isinstance(value, int) and not isinstance(value, bool) and value > 0)
                or (key in {"kiosk", "prevent_blanking", "show_camera_name", "show_timestamp", "schedule_enabled"} and isinstance(value, bool))
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
    parser.add_argument("--bootstrap-token", default=os.getenv("CCTV_BOOTSTRAP_TOKEN", ""))
    parser.add_argument("--public-media-url", default=os.getenv("CCTV_PUBLIC_MEDIA_URL", "http://127.0.0.1:8888"))
    parser.add_argument("--public-webrtc-url", default=os.getenv("CCTV_PUBLIC_WEBRTC_URL", "http://127.0.0.1:8889"))
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.store = Store(args.db)  # type: ignore[attr-defined]
    httpd.admin_token = args.admin_token  # type: ignore[attr-defined]
    httpd.read_token = args.read_token  # type: ignore[attr-defined]
    httpd.recordings_dir = args.recordings_dir  # type: ignore[attr-defined]
    httpd.bootstrap_token = args.bootstrap_token  # type: ignore[attr-defined]
    httpd.public_media_url = args.public_media_url  # type: ignore[attr-defined]
    httpd.public_webrtc_url = args.public_webrtc_url  # type: ignore[attr-defined]
    print(f"Pi CCTV server listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.store.close()  # type: ignore[attr-defined]
        httpd.server_close()


if __name__ == "__main__":
    main()
