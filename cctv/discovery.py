"""Small LAN broadcast responder used for first-boot dashboard discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse


DISCOVERY_PORT = int(os.getenv("CCTV_DISCOVERY_PORT", "42900"))
DISCOVERY_REQUEST = b"PI-CCTV-DISCOVER-v1"


def _local_address(peer: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer, 9))
        return str(probe.getsockname()[0])
    finally:
        probe.close()


def serve() -> None:
    cert_path = Path(os.getenv("CCTV_TLS_CERT", "/run/pi-cctv/tls/server.crt"))
    ca_path = Path(os.getenv("CCTV_TLS_CA_CERT", "/run/pi-cctv/tls/ca.crt"))
    tls_port = int(os.getenv("CCTV_TLS_PORT", "8443"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))
    print(f"Pi CCTV discovery responder listening on UDP {DISCOVERY_PORT}", flush=True)
    while True:
        message, peer = sock.recvfrom(512)
        if message != DISCOVERY_REQUEST:
            continue
        try:
            if not cert_path.is_file() or not ca_path.is_file():
                continue
            ca_fingerprint = hashlib.sha256(ca_path.read_bytes()).hexdigest()
            address = _local_address(peer[0])
            reply = json.dumps({
                "name": os.getenv("CCTV_DISCOVERY_NAME", "Pi CCTV dashboard"),
                "url": f"https://{address}:{tls_port}",
                "media_server": address,
                "ca_sha256": ca_fingerprint,
            }, separators=(",", ":")).encode()
            sock.sendto(reply, peer)
        except (OSError, ValueError) as exc:
            print(f"Discovery response failed for {peer[0]}: {exc}", flush=True)


def discover(timeout: float = 2.0) -> list[dict[str, str]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    try:
        sock.bind(("0.0.0.0", 0))
        sock.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        found: dict[str, dict[str, str]] = {}
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                payload, _peer = sock.recvfrom(2048)
                item = json.loads(payload)
                url = item.get("url", "")
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname and parsed.port and len(item.get("ca_sha256", "")) == 64:
                    found[url] = {
                        "url": url.rstrip("/"), "name": str(item.get("name", parsed.hostname)),
                        "media_server": str(item.get("media_server", parsed.hostname)), "ca_sha256": item["ca_sha256"],
                    }
            except socket.timeout:
                break
            except (OSError, ValueError, TypeError):
                continue
        return list(found.values())
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi CCTV LAN dashboard discovery responder")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    serve()


if __name__ == "__main__":
    main()
