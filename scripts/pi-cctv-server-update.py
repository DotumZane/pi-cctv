#!/usr/bin/python3
"""Apply one fixed, dashboard-requested Pi CCTV server update."""

from __future__ import annotations

import fcntl
import hashlib
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
CONFIG = Path("/etc/pi-cctv-server-update.json")
REQUEST = STATE_DIR / "request.json"
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "host-maintenance.lock"
SOFTWARE_DIR = STATE_DIR / "software"
SOFTWARE_MANIFEST = "pi-cctv.json"
DEFAULT_BRANCH = "main"
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


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
    if not isinstance(payload, dict) or payload.get("action") not in {"update", "check"}:
        return {"id": "invalid", "action": "invalid"}
    request_id = payload.get("id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return {"id": "invalid", "action": "invalid"}
    return payload


def run(command: list[str], cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output[-12000:]


def version_at(repo: Path, ref: str, name: str = "__version__") -> str | None:
    code, source = run(["git", "show", f"{ref}:cctv/__init__.py"], repo, timeout=30)
    if code:
        return None
    match = re.search(rf"^{name}\s*=\s*['\"]([^'\"]+)['\"]", source, re.MULTILINE)
    return match.group(1) if match else None


def configured_branch(config: dict) -> str:
    """Return the release branch this server follows; older configs follow main."""
    branch = config.get("branch", DEFAULT_BRANCH)
    if not isinstance(branch, str) or not BRANCH_PATTERN.fullmatch(branch) or ".." in branch:
        raise ValueError("Configured update branch is not a valid Git branch name")
    return branch


def load_repo(config: dict) -> Path:
    repo = Path(config["project_dir"]).resolve(strict=True)
    if not (repo / "docker-compose.yml").is_file() or not (repo / ".git").exists():
        raise ValueError("Configured server checkout is not a Pi CCTV Git repository")
    return repo


def publish_software(repo: Path) -> dict:
    """Package the server's installed commit so its Pis can update without internet access."""
    commit_code, commit = run(["git", "rev-parse", "HEAD"], repo, timeout=30)
    if commit_code or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise RuntimeError("Could not resolve the installed server commit")
    version = version_at(repo, "HEAD")
    agent_version = version_at(repo, "HEAD", "__agent_version__")
    if not version or not agent_version:
        raise RuntimeError("Could not read the installed software version")
    SOFTWARE_DIR.mkdir(mode=0o2770, parents=True, exist_ok=True)
    bundle_name = f"pi-cctv-{commit}.bundle"
    temporary = SOFTWARE_DIR / f".{bundle_name}.tmp"
    temporary.unlink(missing_ok=True)
    try:
        bundle_code, bundle_log = run(["git", "bundle", "create", str(temporary), "HEAD"], repo, timeout=300)
        if bundle_code:
            raise RuntimeError(f"Could not package Pi software: {bundle_log[-1000:]}")
        digest = hashlib.sha256()
        with temporary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        size = temporary.stat().st_size
        os.replace(temporary, SOFTWARE_DIR / bundle_name)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        "commit": commit,
        "version": version,
        "agent_version": agent_version,
        "bundle": bundle_name,
        "sha256": digest.hexdigest(),
        "size": size,
        "created_at": now(),
    }
    fd, manifest_temporary = tempfile.mkstemp(prefix=".manifest-", dir=SOFTWARE_DIR, text=True)
    try:
        os.fchmod(fd, 0o660)
        with os.fdopen(fd, "w") as stream:
            json.dump(manifest, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(manifest_temporary, SOFTWARE_DIR / SOFTWARE_MANIFEST)
    finally:
        if os.path.exists(manifest_temporary):
            os.unlink(manifest_temporary)
    # A Pi already downloading the previous bundle keeps its open file; only
    # unreferenced packages are removed.
    for old in SOFTWARE_DIR.glob("pi-cctv-*.bundle"):
        if old.name != bundle_name:
            old.unlink(missing_ok=True)
    return manifest


def publish_only() -> int:
    """Publish the current checkout for Pis; used once by the updater installer."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            manifest = publish_software(load_repo(json.loads(CONFIG.read_text())))
        except Exception as exc:
            print(f"Could not publish Pi software: {exc}", file=sys.stderr)
            return 1
    print(f"Published Pi software {manifest['version']} ({manifest['commit'][:12]}) for this dashboard's Pis.")
    return 0


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
        if request.get("action") not in {"update", "check"}:
            status.update(state="failed", finished_at=now(), error="Invalid update request")
            write_status(status)
            return 1
        write_status(status)
        try:
            config = json.loads(CONFIG.read_text())
            repo = load_repo(config)
            release = configured_branch(config)
            status["branch"] = release
            clean, details = run(["git", "status", "--porcelain"], repo, timeout=30)
            if clean != 0 or details:
                raise RuntimeError("Server checkout has local changes; refusing to overwrite them")
            branch_code, branch = run(["git", "branch", "--show-current"], repo, timeout=30)
            if branch_code or branch != release:
                raise RuntimeError(f"Server checkout must be on the {release} branch")
            fetch_code, fetch_log = run(["git", "fetch", "--prune", "origin", release], repo, timeout=180)
            status["log"] = fetch_log
            if fetch_code:
                raise RuntimeError(f"Could not fetch origin/{release}")
            before_code, before = run(["git", "rev-parse", "HEAD"], repo, timeout=30)
            target_code, target = run(["git", "rev-parse", f"origin/{release}"], repo, timeout=30)
            if before_code or target_code:
                raise RuntimeError("Could not resolve local or remote Git commit")
            status["current_commit"] = before[:12]
            status["available_commit"] = target[:12]
            status["current_version"] = version_at(repo, "HEAD")
            status["available_version"] = version_at(repo, f"origin/{release}")
            if request.get("action") == "check":
                status.update(state="idle", checked_at=now(), finished_at=now())
                write_status(status)
                return 0
            if before != target:
                merge_code, merge_log = run(["git", "merge", "--ff-only", f"origin/{release}"], repo, timeout=60)
                status["log"] = (status["log"] + "\n" + merge_log).strip()[-12000:]
                if merge_code:
                    raise RuntimeError("Update is not a fast-forward; manual recovery is required")
            status["message"] = "Building and starting server services"
            write_status(status)
            compose_code, compose_log = run(["docker", "compose", "up", "-d", "--build", "management", "discovery", "media", "apt-cache"], repo, timeout=1800)
            status["log"] = (status["log"] + "\n" + compose_log).strip()[-12000:]
            if compose_code:
                raise RuntimeError("Docker Compose could not build and start the management service")
            final_code, final = run(["git", "rev-parse", "--short=12", "HEAD"], repo, timeout=30)
            final_commit = final if not final_code else target[:12]
            status["message"] = "Packaging software for this dashboard's Pis"
            write_status(status)
            try:
                published = publish_software(repo)
                status["pi_software_commit"] = published["commit"][:12]
                status["message"] = "Server updated; its Pis can now update from this dashboard"
            except Exception as exc:
                # The server itself is updated; Pis fall back to GitHub until
                # the next successful package.
                status["message"] = f"Server updated, but Pi software could not be packaged: {exc}"
            status.update(
                state="succeeded",
                finished_at=now(),
                updated_commit=final_commit,
                current_commit=final_commit,
                available_commit=final_commit,
                current_version=status.get("available_version") or status.get("current_version"),
            )
            write_status(status)
            return 0
        except Exception as exc:  # status is the dashboard's recovery detail
            status.update(state="failed", finished_at=now(), error=str(exc))
            write_status(status)
            return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--publish-software"]:
        raise SystemExit(publish_only())
    raise SystemExit(main())
