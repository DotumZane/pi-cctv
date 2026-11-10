"""Run the fixed, allowlisted Pi CCTV self-update sequence."""

from __future__ import annotations

import subprocess
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path


INSTALL_DIR = Path("/opt/pi-cctv")
DEVICE_STATE = Path("/var/lib/pi-cctv/device.json")
SERVICE = "pi-cctv-agent.service"
HEALTH_TIMEOUT_SECONDS = 60
STABLE_SECONDS = 10
POLL_SECONDS = 2


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def service_main_pid() -> int:
    active = run(["systemctl", "is-active", SERVICE], timeout=10)
    if active.returncode or active.stdout.strip() != "active":
        return 0
    result = run(["systemctl", "show", "--property=MainPID", "--value", SERVICE], timeout=10)
    if result.returncode:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def wait_for_healthy_service() -> bool:
    """Require the restarted agent to keep one live systemd main process."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    stable_pid = 0
    stable_since = 0.0
    while time.monotonic() < deadline:
        pid = service_main_pid()
        now = time.monotonic()
        if pid and pid == stable_pid:
            if now - stable_since >= STABLE_SECONDS:
                return True
        else:
            stable_pid = pid
            stable_since = now
        time.sleep(POLL_SECONDS)
    return False


def current_commit() -> str:
    result = run(["git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR), "rev-parse", "HEAD"], timeout=15)
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        raise RuntimeError("Could not determine the installed software version")
    return commit


def rollback(previous_commit: str) -> bool:
    """Restore the clean pre-update checkout and restart its agent service."""
    reset = run([
        "git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR),
        "reset", "--hard", previous_commit,
    ], timeout=30)
    if reset.returncode:
        print(f"Automatic rollback could not restore {previous_commit[:12]}: {reset.stderr[-1000:]}", flush=True)
        return False
    daemon_reload = run(["systemctl", "daemon-reload"], timeout=30)
    restart = run(["systemctl", "restart", SERVICE], timeout=30) if not daemon_reload.returncode else daemon_reload
    if restart.returncode or not wait_for_healthy_service():
        print("Previous software was restored, but its agent did not pass the health check.", flush=True)
        return False
    return True


def report_status(state: str, message: str) -> None:
    """Report update progress with the enrolled device credential."""
    try:
        device = json.loads(DEVICE_STATE.read_text())
        device_id = str(device["id"])
        server = str(device["server"]).rstrip("/")
        token = str(device["device_token"])
        payload = json.dumps({"state": state, "details": {"message": message[:1000]}}).encode()
        request = urllib.request.Request(
            f"{server}/api/devices/{urllib.parse.quote(device_id, safe='')}/maintenance/agent-update",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception as exc:
        print(f"Unable to report CCTV update status: {exc}", flush=True)


def mark_setup_ap_checked_this_boot() -> None:
    """Keep an agent update restart from reapplying the Wi-Fi bridge profile."""
    try:
        device_id = json.loads(DEVICE_STATE.read_text()).get("id", "")
    except (OSError, ValueError):
        return
    safe_device_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(device_id))[:96] or "device"
    marker_dir = Path("/run/pi-cctv")
    marker = marker_dir / f"setup-ap-synced-{safe_device_id}"
    marker_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="ascii") as marker_file:
            marker_file.write("checked\n")
    except FileExistsError:
        pass


def main() -> int:
    report_status("running", "Downloading the approved CCTV software update.")
    try:
        clean = run(["git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR), "status", "--porcelain"], timeout=15)
        if clean.returncode or clean.stdout.strip():
            raise RuntimeError("Pi software checkout has local changes; update cancelled to protect them")
        previous_commit = current_commit()
        pull = run([
            "git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR),
            "pull", "--ff-only", "origin", "main",
        ], timeout=180)
        if pull.returncode:
            raise RuntimeError(f"Software download failed: {(pull.stderr or pull.stdout).strip()[-1000:]}")
        updated_commit = current_commit()
        if updated_commit == previous_commit:
            report_status("succeeded", "Pi CCTV software is already current; no restart was needed.")
            return 0

        print("Running: systemctl daemon-reload", flush=True)
        daemon_reload = run(["systemctl", "daemon-reload"], timeout=30)
        if daemon_reload.returncode:
            raise RuntimeError("systemd could not reload the updated service configuration")
        mark_setup_ap_checked_this_boot()
        print(f"Running: systemctl restart {SERVICE}", flush=True)
        restart = run(["systemctl", "restart", SERVICE], timeout=30)
        if restart.returncode:
            raise RuntimeError("systemd could not restart the updated Pi CCTV agent")
        if not wait_for_healthy_service():
            raise RuntimeError("updated agent did not stay healthy for 10 seconds")
        report_status("succeeded", "CCTV software updated; the agent restarted and passed its health check.")
        return 0
    except Exception as exc:
        message = str(exc)
        try:
            installed_commit = current_commit()
        except Exception:
            installed_commit = ""
        if "previous_commit" in locals() and installed_commit and installed_commit != previous_commit:
            print(f"Update failed: {message}. Restoring the previous software version.", flush=True)
            if rollback(previous_commit):
                message += f" Automatic rollback restored the previous version ({previous_commit[:12]}), and the agent is healthy."
            else:
                message += f" Automatic rollback attempted to restore {previous_commit[:12]}, but health could not be confirmed; SSH recovery may be needed."
        report_status("failed", message)
        print(message, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
