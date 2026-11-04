"""Minimal device enrollment/heartbeat client for camera and monitor Pis."""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.request

from .runtime import DeviceRuntime


def request(url: str, method: str, payload: dict, token: str = "") -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enroll a Pi CCTV device and send heartbeats")
    parser.add_argument("--role", choices=("camera", "monitor"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--location", default="")
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    # Hostname is stable across service restarts. Operators can provide --id
    # when a site needs an identifier different from the Pi hostname.
    parser.add_argument("--id", default=socket.gethostname())
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--run-media", action="store_true", help="launch and recover the camera/monitor media pipeline")
    parser.add_argument("--media-server", default="127.0.0.1", help="hostname or IP of the MediaMTX server")
    parser.add_argument("--token", default="", help="management API bearer token")
    args = parser.parse_args()
    if args.run_media:
        DeviceRuntime(args.role, args.id, args.name, args.location, args.server, args.media_server, args.interval, token=args.token).run()
        return
    base = args.server.rstrip("/")
    payload = {"id": args.id, "kind": args.role, "name": args.name, "location": args.location}
    request(f"{base}/api/devices", "POST", payload, args.token)
    print(f"Enrolled {args.role} {args.name} as {args.id}", flush=True)
    while True:
        try:
            request(f"{base}/api/devices/{args.id}/heartbeat", "POST", {"hostname": socket.gethostname()}, args.token)
        except Exception as exc:  # device agents must keep retrying through outages
            print(f"Heartbeat failed: {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
