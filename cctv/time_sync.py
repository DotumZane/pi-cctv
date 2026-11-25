"""Make a Pi take its clock from its dashboard server.

systemd-timesyncd tries the dashboard first, then public pool servers, so a
Pi whose older dashboard has no time service still keeps internet time.
timesyncd ignores FallbackNTP once NTP= is set, so the pool servers are listed
explicitly. Nothing here waits on the network.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .apt_cache import cache_host


CONF_DIR = Path("/etc/systemd/timesyncd.conf.d")
CONF_NAME = "90-pi-cctv.conf"
PUBLIC_SERVERS = ("0.debian.pool.ntp.org", "1.debian.pool.ntp.org", "2.debian.pool.ntp.org")
TIMESYNCD_BINARIES = (Path("/usr/lib/systemd/systemd-timesyncd"), Path("/lib/systemd/systemd-timesyncd"))


def configure(server: str, conf_dir: Path | None = None, timesyncd_present: bool | None = None) -> bool:
    """Write the dashboard as this Pi's time server; returns True when it changed."""
    if timesyncd_present is None:
        timesyncd_present = any(path.exists() for path in TIMESYNCD_BINARIES)
    if not timesyncd_present:
        return False  # a Pi using another time client, or a development machine
    host = cache_host(server)
    if host is None:
        return False
    conf_dir = conf_dir or CONF_DIR
    path = conf_dir / CONF_NAME
    content = ("# Installed by the Pi CCTV agent: use the dashboard server's clock first.\n"
               f"[Time]\nNTP={' '.join((host, *PUBLIC_SERVERS))}\n")
    try:
        if path.read_text() == content:
            return False
    except OSError:
        pass
    conf_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{CONF_NAME}-", dir=conf_dir, text=True)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def apply() -> None:
    """Restart timesyncd only if it is already running; never start or block on it."""
    subprocess.run(["systemctl", "try-restart", "systemd-timesyncd.service"],
                   capture_output=True, text=True, timeout=15, check=False)
