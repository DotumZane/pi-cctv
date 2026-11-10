"""Run the fixed, allowlisted Pi CCTV self-update sequence."""

from __future__ import annotations

import subprocess
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path


INSTALL_DIR = Path("/opt/pi-cctv")
DEVICE_STATE = Path("/var/lib/pi-cctv/device.json")


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
    commands = [
        ["git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR), "pull", "--ff-only", "origin", "main"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "restart", "pi-cctv-agent.service"],
    ]
    report_status("running", "Downloading the approved CCTV software update.")
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        if command[-1:] == ["pi-cctv-agent.service"]:
            mark_setup_ap_checked_this_boot()
        result = subprocess.run(command, check=False)
        if result.returncode:
            print(f"Update failed with exit code {result.returncode}", flush=True)
            report_status("failed", f"Update step failed ({' '.join(command[:2])}) with exit code {result.returncode}.")
            return result.returncode
    report_status("succeeded", "CCTV software updated and the agent service restarted successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
