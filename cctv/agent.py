"""Minimal device enrollment/heartbeat client for camera and monitor Pis."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from .discovery import discover

from . import __agent_version__
from .runtime import DeviceRuntime, connected_displays, system_metrics


DEVICE_STATE_FILE = Path(os.environ.get("CCTV_AGENT_STATE_FILE", "/var/lib/pi-cctv/device.json"))


def save_device_state(state: dict, path: Path = DEVICE_STATE_FILE) -> None:
    """Persist the one-time enrollment token for later service restarts."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".device-", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_device_state(path: Path = DEVICE_STATE_FILE) -> dict | None:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    required = ("kind", "id", "name", "server", "media_server", "device_token")
    return state if isinstance(state, dict) and all(state.get(key) for key in required) and "location" in state else None


def request(url: str, method: str, payload: dict | None = None, token: str = "", bootstrap: str = "", pairing: str = "", context: ssl.SSLContext | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if bootstrap:
        headers["X-Bootstrap-Token"] = bootstrap
    if pairing:
        headers["X-Device-Pairing"] = pairing
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=context) as response:
        return json.load(response)


def save_pairing(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pairing-", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _trusted_server(server: dict[str, str], ca_dir: Path) -> ssl.SSLContext:
    ca_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_path = ca_dir / f"ca-{server['ca_sha256'][:16]}.crt"
    if not ca_path.exists():
        request = urllib.request.Request(f"{server['url']}/ca.crt")
        with urllib.request.urlopen(request, timeout=8, context=ssl._create_unverified_context()) as response:
            cert = response.read(16384)
        if hashlib.sha256(cert).hexdigest() != server["ca_sha256"]:
            raise ValueError("dashboard certificate authority fingerprint did not match LAN discovery")
        temporary = ca_path.with_suffix(".tmp")
        temporary.write_bytes(cert)
        os.chmod(temporary, 0o600)
        os.replace(temporary, ca_path)
    return ssl.create_default_context(cafile=str(ca_path))


def run_discovered(state_path: Path = DEVICE_STATE_FILE) -> None:
    saved = load_device_state(state_path)
    if saved:
        DeviceRuntime(saved["kind"], saved["id"], saved["name"], saved["location"], saved["server"], saved["media_server"], token=saved["device_token"]).run(enroll=False)
        return
    pairing_path = state_path.with_name("pairing.json")
    try:
        pairing = json.loads(pairing_path.read_text())
        if not pairing.get("id") or len(pairing.get("secret", "")) < 32:
            raise ValueError("invalid pairing state")
    except (OSError, ValueError, TypeError):
        pairing = {"id": socket.gethostname(), "secret": secrets.token_urlsafe(32)}
        save_pairing(pairing_path, pairing)
    ca_dir = state_path.parent / "server-cas"
    while True:
        candidates = discover()
        if not candidates:
            print("No Pi CCTV dashboards discovered on this LAN; retrying", flush=True)
        for server in candidates:
            try:
                context = _trusted_server(server, ca_dir)
                metadata = {"hostname": socket.gethostname(), "agent_version": __agent_version__}
                response = request(
                    f"{server['url']}/api/enroll/discovered", "POST",
                    {"id": pairing["id"], "name": pairing["id"], "metadata": metadata},
                    pairing=pairing["secret"], context=context,
                )
                claim = request(
                    f"{server['url']}/api/enroll/discovered/{pairing['id']}", "GET",
                    pairing=pairing["secret"], context=context,
                )
                if claim.get("status") == "adopted":
                    for other in candidates:
                        if other["url"] == server["url"]:
                            continue
                        try:
                            other_context = _trusted_server(other, ca_dir)
                            request(
                                f"{other['url']}/api/enroll/discovered/{pairing['id']}/cancel", "POST",
                                pairing=pairing["secret"], context=other_context,
                            )
                        except Exception:
                            pass
                    enrolled = {
                        "kind": claim["kind"], "id": claim["id"], "name": claim["name"],
                        "location": claim.get("location", ""), "server": server["url"],
                        "media_server": server["media_server"], "device_token": claim["device_token"],
                    }
                    if enrolled["id"] != socket.gethostname():
                        subprocess.run(["hostnamectl", "set-hostname", enrolled["id"]], check=True, timeout=15)
                    save_device_state(enrolled, state_path)
                    pairing_path.unlink(missing_ok=True)
                    DeviceRuntime(enrolled["kind"], enrolled["id"], enrolled["name"], enrolled["location"], enrolled["server"], enrolled["media_server"], token=enrolled["device_token"]).run(enroll=False)
                    return
                print(f"Waiting for adoption on dashboard {server['name']} ({pairing['id']})", flush=True)
            except Exception as exc:
                print(f"Dashboard {server['name']} not ready: {exc}", flush=True)
        time.sleep(10)


def run_pending(server: str, bootstrap: str, media_server: str, name: str = "", state_path: Path = DEVICE_STATE_FILE) -> None:
    saved = load_device_state(state_path)
    if saved:
        runtime = DeviceRuntime(saved["kind"], saved["id"], saved["name"], saved["location"], saved["server"], saved["media_server"], token=saved["device_token"])
        runtime.run(enroll=False)
        return
    device_id = socket.gethostname()
    base = server.rstrip("/")
    while True:
        try:
            request(f"{base}/api/enroll", "POST", {"id": device_id, "name": name or device_id, "metadata": {"hostname": device_id}}, bootstrap=bootstrap)
            state = request(f"{base}/api/enroll/{device_id}", "GET", bootstrap=bootstrap)
            if state.get("status") == "adopted":
                enrolled = {
                    "kind": state["kind"], "id": state["id"], "name": state["name"],
                    "location": state.get("location", ""), "server": server,
                    "media_server": media_server, "device_token": state["device_token"],
                }
                if enrolled["id"] != socket.gethostname():
                    subprocess.run(["hostnamectl", "set-hostname", enrolled["id"]], check=True, timeout=15)
                save_device_state(enrolled, state_path)
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
    parser.add_argument("--discover", action="store_true", help="discover local dashboards and wait for adoption without a shared enrollment token")
    parser.add_argument("--bootstrap-token", default="", help="pending enrollment token")
    args = parser.parse_args()
    if args.discover:
        run_discovered()
        return
    if args.enroll:
        if not args.bootstrap_token and not load_device_state():
            parser.error("--enroll requires --bootstrap-token")
        run_pending(args.server, args.bootstrap_token, args.media_server, args.name)
        return
    if not args.role or not args.name:
        parser.error("--role and --name are required unless --enroll is used")
    if args.run_media:
        saved = load_device_state()
        if saved:
            runtime = DeviceRuntime(saved["kind"], saved["id"], saved["name"], saved["location"], saved["server"], saved["media_server"], args.interval, token=saved["device_token"])
        else:
            saved = {
                "kind": args.role, "id": args.id, "name": args.name,
                "location": args.location, "server": args.server.rstrip("/"),
                "media_server": args.media_server.rstrip("/"), "device_token": args.token,
            }
            save_device_state(saved)
            runtime = DeviceRuntime(args.role, args.id, args.name, args.location, args.server, args.media_server, args.interval, token=args.token)
        runtime.run()
        return
    base = args.server.rstrip("/")
    payload = {"id": args.id, "kind": args.role, "name": args.name, "location": args.location}
    request(f"{base}/api/devices", "POST", payload, args.token)
    print(f"Enrolled {args.role} {args.name} as {args.id}", flush=True)
    while True:
        try:
            heartbeat = {"hostname": socket.gethostname(), "agent_version": __agent_version__, "metrics": system_metrics()}
            if args.role == "monitor":
                heartbeat["displays"] = connected_displays()
            request(f"{base}/api/devices/{args.id}/heartbeat", "POST", heartbeat, args.token)
        except Exception as exc:  # device agents must keep retrying through outages
            print(f"Heartbeat failed: {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
