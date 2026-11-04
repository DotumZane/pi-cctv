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

    def authorized(self, admin_only: bool = False) -> bool:
        admin_token = getattr(self.server, "admin_token", "")  # type: ignore[attr-defined]
        read_token = getattr(self.server, "read_token", "")  # type: ignore[attr-defined]
        if not admin_token and not read_token:
            return True
        supplied = self.headers.get("Authorization", "")
        if admin_token and secrets.compare_digest(supplied, f"Bearer {admin_token}"):
            return True
        return not admin_only and bool(read_token) and secrets.compare_digest(supplied, f"Bearer {read_token}")

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
            self.send_json(assignment or {"monitor_id": monitor_id, "layout": "single", "cameras": "[]"})
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
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
            allowed = {"width", "height", "fps", "bitrate"}
            if not camera or camera["kind"] != "camera":
                self.send_json({"error": "camera not found"}, HTTPStatus.NOT_FOUND)
            elif any(key not in allowed for key in settings) or not all(isinstance(value, int) and value > 0 for value in settings.values()):
                self.send_json({"error": "settings must contain only positive integer width, height, fps, or bitrate values"}, HTTPStatus.BAD_REQUEST)
            else:
                device = self.store.update_settings(camera_id, settings)
                self.store.audit("camera.settings", camera_id, self.client_address[0], settings)
                self.send_json(device)
            return
        if parsed.path.startswith("/api/monitors/") and parsed.path.endswith("/assignment"):
            if not self.require_auth():
                return
            monitor_id = parsed.path.split("/")[3]
            payload = self.body()
            monitor = self.store.device(monitor_id)
            cameras = payload.get("cameras", [])
            camera_ids = {device["id"] for device in self.store.devices("camera")}
            if not monitor or monitor["kind"] != "monitor":
                self.send_json({"error": "monitor not found"}, HTTPStatus.NOT_FOUND)
            elif payload.get("layout") not in ("single", "2x2") or not isinstance(cameras, list):
                self.send_json({"error": "layout must be single or 2x2 and cameras must be a list"}, HTTPStatus.BAD_REQUEST)
            elif len(cameras) > (1 if payload["layout"] == "single" else 4) or any(camera not in camera_ids for camera in cameras):
                self.send_json({"error": "invalid camera assignment"}, HTTPStatus.BAD_REQUEST)
            else:
                assignment = self.store.set_assignment(monitor_id, payload["layout"], cameras)
                self.store.audit("monitor.assignment", monitor_id, self.client_address[0], {"layout": payload["layout"], "cameras": cameras})
                self.send_json(assignment)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
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
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.store = Store(args.db)  # type: ignore[attr-defined]
    httpd.admin_token = args.admin_token  # type: ignore[attr-defined]
    httpd.read_token = args.read_token  # type: ignore[attr-defined]
    httpd.recordings_dir = args.recordings_dir  # type: ignore[attr-defined]
    httpd.bootstrap_token = args.bootstrap_token  # type: ignore[attr-defined]
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
