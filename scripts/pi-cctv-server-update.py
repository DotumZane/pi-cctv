#!/usr/bin/python3
"""Apply one fixed, dashboard-requested Pi CCTV server update."""

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
CONFIG = Path("/etc/pi-cctv-server-update.json")
REQUEST = STATE_DIR / "request.json"
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "host-maintenance.lock"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(value: dict) -> None:
    STATE_DIR.mkdir(mode=0o770, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=STATE_DIR, text=True)
    try:
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
            file_info = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_size > 4096:
                raise ValueError("request file is not a small regular file")
            payload = json.load(stream)
    finally:
        REQUEST.unlink(missing_ok=True)
    if not isinstance(payload, dict) or payload.get("action") != "update":
        return {"id": "invalid", "action": "invalid"}
    request_id = payload.get("id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return {"id": "invalid", "action": "invalid"}
    return payload


def run(command: list[str], cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output[-12000:]


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            request = consume_request()
        except Exception as exc:
            write_status({"state": "failed", "finished_at": now(), "error": f"Invalid update request: {exc}"})
            return 1
        if not request:
            return 0
        request_id = request["id"]
        status = {"request_id": request_id, "state": "running", "started_at": now(), "log": ""}
        if request.get("action") != "update":
            status.update(state="failed", finished_at=now(), error="Invalid update request")
            write_status(status)
            return 1
        try:
            config = json.loads(CONFIG.read_text())
            repo = Path(config["project_dir"]).resolve(strict=True)
            if not (repo / "docker-compose.yml").is_file() or not (repo / ".git").exists():
                raise ValueError("Configured server checkout is not a Pi CCTV Git repository")
            clean, details = run(["git", "status", "--porcelain"], repo, timeout=30)
            if clean != 0 or details:
                raise RuntimeError("Server checkout has local changes; refusing to overwrite them")
            branch_code, branch = run(["git", "branch", "--show-current"], repo, timeout=30)
            if branch_code or branch != "main":
                raise RuntimeError("Server checkout must be on the main branch")
            fetch_code, fetch_log = run(["git", "fetch", "--prune", "origin", "main"], repo, timeout=180)
            status["log"] = fetch_log
            if fetch_code:
                raise RuntimeError("Could not fetch origin/main")
            before_code, before = run(["git", "rev-parse", "HEAD"], repo, timeout=30)
            target_code, target = run(["git", "rev-parse", "origin/main"], repo, timeout=30)
            if before_code or target_code:
                raise RuntimeError("Could not resolve local or remote Git commit")
            status["current_commit"] = before[:12]
            status["available_commit"] = target[:12]
            if before != target:
                merge_code, merge_log = run(["git", "merge", "--ff-only", "origin/main"], repo, timeout=60)
                status["log"] = (status["log"] + "\n" + merge_log).strip()[-12000:]
                if merge_code:
                    raise RuntimeError("Update is not a fast-forward; manual recovery is required")
            compose_code, compose_log = run(["docker", "compose", "up", "-d", "--build", "management", "discovery", "media"], repo, timeout=1800)
            status["log"] = (status["log"] + "\n" + compose_log).strip()[-12000:]
            if compose_code:
                raise RuntimeError("Docker Compose could not build and start the management service")
            final_code, final = run(["git", "rev-parse", "--short=12", "HEAD"], repo, timeout=30)
            status.update(state="succeeded", finished_at=now(), updated_commit=final if not final_code else target[:12])
            write_status(status)
            return 0
        except Exception as exc:  # status is the dashboard's recovery detail
            status.update(state="failed", finished_at=now(), error=str(exc))
            write_status(status)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
