"""Run the fixed, allowlisted Pi CCTV self-update sequence."""

from __future__ import annotations

import subprocess
from pathlib import Path


INSTALL_DIR = Path("/opt/pi-cctv")


def main() -> int:
    commands = [
        ["git", "-c", f"safe.directory={INSTALL_DIR}", "-C", str(INSTALL_DIR), "pull", "--ff-only", "origin", "main"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "restart", "pi-cctv-agent.service"],
    ]
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            print(f"Update failed with exit code {result.returncode}", flush=True)
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
