"""Point a Pi's OS package downloads at its dashboard's package cache.

apt runs a small detect helper before each HTTP download. The helper uses the
dashboard's cache on port 3142 only when it answers; otherwise apt connects
directly, so older servers without a cache and switch-only operation behave
exactly as before. Nothing here waits on the network.
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.parse
from pathlib import Path


APT_CONF_DIR = Path("/etc/apt/apt.conf.d")
APT_CONF_NAME = "90pi-cctv-package-cache"
HOST_FILE = Path("/etc/pi-cctv/apt-cache-host")
DETECT_HELPER = Path("/usr/local/lib/pi-cctv/apt-cache-detect")
CACHE_PORT = 3142
HOST_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?")

DETECT_SCRIPT = f"""#!/bin/bash
# Installed by the Pi CCTV agent. apt runs this before HTTP downloads: use the
# dashboard's package cache when it answers, otherwise connect directly.
host="$(head -c 255 {HOST_FILE} 2>/dev/null | tr -d '[:space:]')"
if [[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] && timeout 2 bash -c ">/dev/tcp/$host/{CACHE_PORT}" 2>/dev/null; then
  echo "http://$host:{CACHE_PORT}"
else
  echo DIRECT
fi
"""

APT_CONF = f'Acquire::http::Proxy-Auto-Detect "{DETECT_HELPER}";\n'


def cache_host(server: str) -> str | None:
    """Return the dashboard host that also runs the package cache."""
    host = urllib.parse.urlsplit(server).hostname or ""
    if not HOST_PATTERN.fullmatch(host) or ".." in host or host in {"localhost"} or host.startswith("127."):
        return None
    return host


def _write_if_changed(path: Path, content: str, mode: int) -> bool:
    try:
        if path.read_text() == content and (path.stat().st_mode & 0o777) == mode:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def configure(server: str, apt_conf_dir: Path | None = None, host_file: Path | None = None,
              helper: Path | None = None) -> bool:
    """Install or refresh the package-cache settings; returns True when anything changed."""
    apt_conf_dir = apt_conf_dir or APT_CONF_DIR
    if not apt_conf_dir.is_dir():
        return False  # not a Debian-based Pi (for example a development machine)
    host = cache_host(server)
    if host is None:
        return False
    host_file = host_file or HOST_FILE
    helper = helper or DETECT_HELPER
    script = DETECT_SCRIPT.replace(str(HOST_FILE), str(host_file))
    conf = APT_CONF.replace(str(DETECT_HELPER), str(helper))
    changed = _write_if_changed(helper, script, 0o755)
    changed = _write_if_changed(host_file, host + "\n", 0o644) or changed
    return _write_if_changed(apt_conf_dir / APT_CONF_NAME, conf, 0o644) or changed
