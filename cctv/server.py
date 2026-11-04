"""Dependency-free V1 management API and browser dashboard."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

    def authorized(self) -> bool:
        expected = getattr(self.server, "admin_token", "")  # type: ignore[attr-defined]
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        return secrets.compare_digest(supplied, f"Bearer {expected}")

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
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
            self.send_json({"devices": self.store.devices(parse_qs(parsed.query).get("kind", [None])[0])})
            return
        if parsed.path.startswith("/api/devices/"):
            device = self.store.device(parsed.path.split("/")[3])
            self.send_json(device or {"error": "device not found"}, HTTPStatus.OK if device else HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/assignments":
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
        if parsed.path.startswith("/api/monitors/") and parsed.path.endswith("/assignment"):
            monitor_id = parsed.path.split("/")[3]
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
            if parsed.path.startswith("/api/devices/") and parsed.path.endswith("/heartbeat"):
                if not self.require_auth():
                    return
                device_id = parsed.path.split("/")[3]
                device = self.store.heartbeat(device_id, self.client_address[0], json.dumps(self.body()))
                self.send_json(device or {"error": "device not found"}, HTTPStatus.OK if device else HTTPStatus.NOT_FOUND)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi CCTV V1 management server")
    parser.add_argument("--host", default=os.getenv("CCTV_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CCTV_PORT", "8080")))
    parser.add_argument("--db", default=os.getenv("CCTV_DB", "data/cctv.db"))
    parser.add_argument("--admin-token", default=os.getenv("CCTV_ADMIN_TOKEN", ""))
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.store = Store(args.db)  # type: ignore[attr-defined]
    httpd.admin_token = args.admin_token  # type: ignore[attr-defined]
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
