#!/usr/bin/python3
"""Apply fixed Ubuntu package upgrades requested by the dashboard."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


STATE_DIR = Path("/var/lib/pi-cctv-host-update")
REQUEST = STATE_DIR / "os-request.json"
STATUS = STATE_DIR / "os-status.json"
LOCK = STATE_DIR / "host-maintenance.lock"
OS_RELEASE = Path("/etc/os-release")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".os-status-", dir=STATE_DIR, text=True)
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
    if not isinstance(payload, dict) or payload.get("action") != "os_update":
        return {"id": "invalid", "action": "invalid"}
    request_id = payload.get("id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return {"id": "invalid", "action": "invalid"}
    return payload


def run(command: list[str], output: list[str], timeout: int) -> None:
    environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a"}
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False, env=environment)
    text = (result.stdout + result.stderr).strip()
    if text:
        output.append(f"$ {' '.join(command)}\n{text}")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}")


def main() -> int:
    STATE_DIR.mkdir(mode=0o770, parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            request = consume_request()
        except Exception as exc:
            write_status({"state": "failed", "finished_at": now(), "error": f"Invalid update request: {exc}"})
            return 1
        if not request:
            return 0
        status = {"request_id": request["id"], "state": "running", "started_at": now(), "log": ""}
        write_status(status)
        output: list[str] = []
        try:
            if request.get("action") != "os_update":
                raise ValueError("Invalid OS update action")
            if not OS_RELEASE.is_file():
                raise RuntimeError("Could not identify the host operating system")
            run(["apt-get", "update"], output, timeout=1800)
            run(["apt-get", "-y", "upgrade"], output, timeout=3600)
            reboot_required = Path("/var/run/reboot-required").exists()
            status.update(
                state="succeeded",
                finished_at=now(),
                reboot_required=reboot_required,
                log="\n\n".join(output)[-12000:],
            )
            write_status(status)
            return 0
        except Exception as exc:
            status.update(state="failed", finished_at=now(), error=str(exc), log="\n\n".join(output)[-12000:])
            write_status(status)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
