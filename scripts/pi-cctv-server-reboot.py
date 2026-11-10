#!/usr/bin/python3
"""Reboot the Ubuntu dashboard host after an authenticated dashboard request."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


STATE_DIR = Path("/var/lib/pi-cctv-host-update")
REQUEST = STATE_DIR / "reboot-request.json"
STATUS = STATE_DIR / "reboot-status.json"
LOCK = STATE_DIR / "host-maintenance.lock"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def boot_id() -> str:
    return BOOT_ID.read_text().strip()


def write_status(value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".reboot-status-", dir=STATE_DIR, text=True)
    try:
        os.fchown(fd, -1, os.stat(STATE_DIR).st_gid)
        os.fchmod(fd, 0o660)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATUS)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def consume_request() -> dict | None:
    try:
        fd = os.open(REQUEST, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError("request must be a small regular file")
            payload = json.load(stream)
    finally:
        REQUEST.unlink(missing_ok=True)
    if not isinstance(payload, dict) or payload.get("action") != "reboot":
        return {"id": "invalid", "action": "invalid"}
    request_id = payload.get("id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return {"id": "invalid", "action": "invalid"}
    return payload


def complete_previous_reboot() -> int:
    try:
        status = json.loads(STATUS.read_text())
        current_boot = boot_id()
    except (OSError, json.JSONDecodeError):
        return 0
    if status.get("state") == "rebooting" and status.get("boot_id") != current_boot:
        status.update(state="succeeded", finished_at=now(), message="Server reboot completed")
        write_status(status)
    return 0


def main() -> int:
    if sys.argv[1:] == ["--complete"]:
        return complete_previous_reboot()
    STATE_DIR.mkdir(mode=0o770, parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            request = consume_request()
        except Exception as exc:
            write_status({"state": "failed", "finished_at": now(), "error": f"Invalid reboot request: {exc}"})
            return 1
        if not request:
            return 0
        status = {
            "request_id": request["id"],
            "state": "rebooting",
            "started_at": now(),
            "boot_id": boot_id(),
            "message": "Reboot requested; dashboard will reconnect when the server is back",
        }
        write_status(status)
        try:
            result = subprocess.run(["systemctl", "reboot"], text=True, capture_output=True, timeout=60, check=False)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "systemctl reboot failed").strip())
            return 0
        except Exception as exc:
            status.update(state="failed", finished_at=now(), error=str(exc))
            write_status(status)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
