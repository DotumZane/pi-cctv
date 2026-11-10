"""Run a fixed, unattended Debian/Raspberry Pi OS package update."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


DEVICE_STATE_FILE = Path(os.environ.get("CCTV_AGENT_STATE_FILE", "/var/lib/pi-cctv/device.json"))
COMMAND_TIMEOUT_SECONDS = 1800


def report(state: dict, status: str, output: str = "", reboot_required: bool = False) -> None:
    payload = json.dumps({
        "state": status,
        "details": {"output": output[-12000:], "reboot_required": reboot_required},
    }).encode()
    request = urllib.request.Request(
        f"{state['server'].rstrip('/')}/api/devices/{state['id']}/maintenance/os-update",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {state['device_token']}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def run_update(state_path: Path = DEVICE_STATE_FILE) -> int:
    try:
        state = json.loads(state_path.read_text())
        if not all(state.get(key) for key in ("id", "server", "device_token")):
            raise ValueError("device state is missing required enrollment fields")
    except (OSError, ValueError, TypeError) as exc:
        print(f"Cannot load enrolled device state: {exc}", file=sys.stderr, flush=True)
        return 2

    output: list[str] = []
    try:
        report(state, "running")
        environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        commands = (
            ["apt-get", "update"],
            ["apt-get", "-y", "upgrade", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"],
        )
        for command in commands:
            output.append("$ " + " ".join(command))
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env=environment,
            )
            output.append(result.stdout or "")
            output.append(result.stderr or "")
            if result.returncode:
                raise RuntimeError(f"{command[0]} exited with status {result.returncode}")
        combined = "\n".join(output)[-12000:]
        reboot_required = Path("/var/run/reboot-required").exists()
        report(state, "succeeded", combined, reboot_required)
        print("Pi OS package update completed." + (" Reboot required." if reboot_required else ""), flush=True)
        return 0
    except Exception as exc:
        combined = ("\n".join(output) + f"\nUpdate failed: {exc}")[-12000:]
        try:
            report(state, "failed", combined, Path("/var/run/reboot-required").exists())
        except Exception as report_error:
            print(f"Unable to report Pi OS update status: {report_error}", file=sys.stderr, flush=True)
        print(combined, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_update())
