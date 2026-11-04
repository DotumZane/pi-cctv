"""Provision a representative virtual CCTV fleet against a running V1 server."""

from __future__ import annotations

import argparse
import json
import urllib.request


def request(base: str, method: str, path: str, payload: dict, token: str = "") -> dict:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def provision(base: str, cameras: int, monitors: int, token: str = "") -> dict:
    camera_count = cameras
    for index in range(1, cameras + 1):
        request(base, "POST", "/api/devices", {"id": f"cam-{index:02d}", "kind": "camera", "name": f"Camera {index:02d}", "location": f"Line {(index - 1) % 4 + 1}"}, token)
    for index in range(1, monitors + 1):
        request(base, "POST", "/api/devices", {"id": f"mon-{index:02d}", "kind": "monitor", "name": f"Monitor {index:02d}", "location": f"Area {(index - 1) % 4 + 1}"}, token)
    for index in range(1, monitors + 1):
        if index % 3 == 0:
            camera_ids = [f"cam-{((index - 1) * 4 + slot) % camera_count + 1:02d}" for slot in range(4)]
            layout = "2x2"
        else:
            camera_ids = [f"cam-{(index - 1) % camera_count + 1:02d}"]
            layout = "single"
        request(base, "PUT", f"/api/monitors/mon-{index:02d}/assignment", {"layout": layout, "cameras": camera_ids}, token)
    return request(base, "GET", "/api/health", {}, token)


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision and exercise a virtual Pi CCTV fleet")
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--cameras", type=int, default=26)
    parser.add_argument("--monitors", type=int, default=23)
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    print(json.dumps(provision(args.server, args.cameras, args.monitors, args.token), indent=2))
