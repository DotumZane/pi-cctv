"""Minimal device enrollment/heartbeat client for camera and monitor Pis."""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.request

from .runtime import DeviceRuntime, system_metrics


def request(url: str, method: str, payload: dict | None = None, token: str = "", bootstrap: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if bootstrap:
        headers["X-Bootstrap-Token"] = bootstrap
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def run_pending(server: str, bootstrap: str, media_server: str, name: str = "") -> None:
    device_id = socket.gethostname()
    base = server.rstrip("/")
    while True:
        try:
            request(f"{base}/api/enroll", "POST", {"id": device_id, "name": name or device_id, "metadata": {"hostname": device_id}}, bootstrap=bootstrap)
            state = request(f"{base}/api/enroll/{device_id}", "GET", bootstrap=bootstrap)
            if state.get("status") == "adopted":
                runtime = DeviceRuntime(state["kind"], device_id, state["name"], state.get("location", ""), server, media_server, token=state["device_token"])
                runtime.run(enroll=False)
                return
            print(f"Waiting for dashboard adoption of {device_id}", flush=True)
        except Exception as exc:
            print(f"Pending enrollment failed: {exc}; retrying", flush=True)
        time.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enroll a Pi CCTV device and send heartbeats")
    parser.add_argument("--role", choices=("camera", "monitor"))
    parser.add_argument("--name", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    # Hostname is stable across service restarts. Operators can provide --id
    # when a site needs an identifier different from the Pi hostname.
    parser.add_argument("--id", default=socket.gethostname())
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--run-media", action="store_true", help="launch and recover the camera/monitor media pipeline")
    parser.add_argument("--media-server", default="127.0.0.1", help="hostname or IP of the MediaMTX server")
    parser.add_argument("--token", default="", help="management API bearer token")
    parser.add_argument("--enroll", action="store_true", help="register as a pending device and wait for dashboard adoption")
    parser.add_argument("--bootstrap-token", default="", help="pending enrollment token")
    args = parser.parse_args()
    if args.enroll:
        if not args.bootstrap_token:
            parser.error("--enroll requires --bootstrap-token")
        run_pending(args.server, args.bootstrap_token, args.media_server, args.name)
        return
    if not args.role or not args.name:
        parser.error("--role and --name are required unless --enroll is used")
    if args.run_media:
        DeviceRuntime(args.role, args.id, args.name, args.location, args.server, args.media_server, args.interval, token=args.token).run()
        return
    base = args.server.rstrip("/")
    payload = {"id": args.id, "kind": args.role, "name": args.name, "location": args.location}
    request(f"{base}/api/devices", "POST", payload, args.token)
    print(f"Enrolled {args.role} {args.name} as {args.id}", flush=True)
    while True:
        try:
            request(f"{base}/api/devices/{args.id}/heartbeat", "POST", {"hostname": socket.gethostname(), "metrics": system_metrics()}, args.token)
        except Exception as exc:  # device agents must keep retrying through outages
            print(f"Heartbeat failed: {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
