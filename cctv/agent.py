"""Minimal device enrollment/heartbeat client for camera and monitor Pis."""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.request
import uuid


def request(url: str, method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enroll a Pi CCTV device and send heartbeats")
    parser.add_argument("--role", choices=("camera", "monitor"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--location", default="")
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--id", default=f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()
    base = args.server.rstrip("/")
    payload = {"id": args.id, "kind": args.role, "name": args.name, "location": args.location}
    request(f"{base}/api/devices", "POST", payload)
    print(f"Enrolled {args.role} {args.name} as {args.id}", flush=True)
    while True:
        try:
            request(f"{base}/api/devices/{args.id}/heartbeat", "POST", {"hostname": socket.gethostname()})
        except Exception as exc:  # device agents must keep retrying through outages
            print(f"Heartbeat failed: {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
