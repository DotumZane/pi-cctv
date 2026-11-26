"""The one-command installer a dashboard serves to brand-new Pis at /install.

A Pi on an isolated CCTV network runs ``curl -fsSL http://SERVER:8080/install
| sudo bash``. The script points the Pi at its dashboard for the clock, OS
packages, Pi CCTV software and the camera relay download, then runs the normal
installer in discovery mode, so the Pi needs no internet access of its own.
"""

from __future__ import annotations

import re
import shlex

from . import apt_cache, time_sync


DEFAULT_REPO_URL = "https://github.com/DotumZane/pi-cctv.git"
SOFTWARE_SERVER_FILE = "/etc/pi-cctv/software-server"
PORT_PATTERN = re.compile(r"[0-9]{1,5}")


def dashboard_origin(host_header: str, https: bool) -> tuple[str, str] | None:
    """Return (origin, host) for the address the Pi used, or None if unusable."""
    host, _, port = (host_header or "").strip().rpartition(":")
    if not host:
        host, port = port, ""
    if port and (not PORT_PATTERN.fullmatch(port) or not 0 < int(port) < 65536):
        return None
    if apt_cache.cache_host(f"http://{host}") != host:
        return None  # bracketed IPv6, localhost or anything unexpected
    scheme = "https" if https else "http"
    return f"{scheme}://{host}{':' + port if port else ''}", host


def script(origin: str, host: str, repo_url: str = DEFAULT_REPO_URL) -> str:
    return TEMPLATE.format(
        server=shlex.quote(origin),
        host=shlex.quote(host),
        repo=shlex.quote(repo_url),
        software_server_file=SOFTWARE_SERVER_FILE,
        apt_conf=f"{apt_cache.APT_CONF_DIR}/{apt_cache.APT_CONF_NAME}",
        apt_conf_text=apt_cache.APT_CONF,
        host_file=apt_cache.HOST_FILE,
        helper=apt_cache.DETECT_HELPER,
        helper_dir=apt_cache.DETECT_HELPER.parent,
        detect_script=apt_cache.DETECT_SCRIPT,
        time_conf=f"{time_sync.CONF_DIR}/{time_sync.CONF_NAME}",
        time_conf_dir=time_sync.CONF_DIR,
        time_conf_text=time_sync.config_text(host),
    )


# Everything runs inside main() so bash has read the whole script before any
# command can read from the curl pipe.
TEMPLATE = r"""#!/usr/bin/env bash
# Pi CCTV installer served by the dashboard. It needs no internet access on the Pi.
set -euo pipefail

SERVER={server}
HOST={host}
REPO_URL={repo}
INSTALL_DIR=/opt/pi-cctv

main() {{
  if [[ "${{EUID}}" -ne 0 ]]; then
    echo "Run as root: curl -fsSL $SERVER/install | sudo bash" >&2
    exit 1
  fi
  command -v python3 >/dev/null || {{ echo "python3 is required; use Raspberry Pi OS Lite." >&2; exit 1; }}
  echo "Installing Pi CCTV from the dashboard at $SERVER"

  install -d -m 0755 /etc/pi-cctv
  printf '%s\n' "$SERVER" > {software_server_file}

  if [[ -d /etc/apt/apt.conf.d ]]; then
    echo "Using the dashboard's package cache for OS packages."
    install -d -m 0755 {helper_dir}
    printf '%s\n' "$HOST" > {host_file}
    cat > {helper} <<'PI_CCTV_DETECT'
{detect_script}PI_CCTV_DETECT
    chmod 0755 {helper}
    cat > {apt_conf} <<'PI_CCTV_APT'
{apt_conf_text}PI_CCTV_APT
  fi

  if [[ -x /usr/lib/systemd/systemd-timesyncd || -x /lib/systemd/systemd-timesyncd ]]; then
    # A Pi has no battery clock; package lists are rejected as "not valid yet"
    # until the clock is right, so take the time from the dashboard first.
    install -d -m 0755 {time_conf_dir}
    cat > {time_conf} <<'PI_CCTV_TIME'
{time_conf_text}PI_CCTV_TIME
    timedatectl set-ntp true >/dev/null 2>&1 || true
    systemctl restart systemd-timesyncd.service >/dev/null 2>&1 || true
    echo "Waiting up to 30 seconds for the clock to sync with the dashboard..."
    local synced=no
    for _ in $(seq 1 30); do
      synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo no)"
      [[ "$synced" == yes ]] && break
      sleep 1
    done
    if [[ "$synced" == yes ]]; then
      echo "Clock synced: $(date)"
    else
      echo "Warning: the clock has not synced yet ($(date)); continuing." >&2
    fi
  fi

  bundle="$(mktemp /tmp/pi-cctv-XXXXXX.bundle)"
  trap 'rm -f "$bundle"' EXIT
  echo "Downloading the dashboard's Pi CCTV software..."
  commit="$(python3 - "$SERVER" "$bundle" <<'PI_CCTV_PY'
import hashlib, json, re, sys, urllib.error, urllib.request
server, path = sys.argv[1:]
try:
    with urllib.request.urlopen(server + "/software/pi-cctv.json", timeout=30) as response:
        manifest = json.loads(response.read(65536))
except urllib.error.HTTPError as exc:
    try:
        detail = json.loads(exc.read(4096)).get("error", "")
    except Exception:
        detail = ""
    sys.exit("The dashboard has no Pi software to install" + (": " + detail if detail else f" (HTTP {{exc.code}})"))
except Exception as exc:
    sys.exit(f"Could not reach the dashboard at {{server}}: {{exc}}")
commit, digest, size = manifest.get("commit"), manifest.get("sha256"), manifest.get("size")
if (not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{{40}}", commit)
        or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{{64}}", digest)
        or not isinstance(size, int) or not 0 < size <= 256 * 1024 * 1024):
    sys.exit("The dashboard returned an invalid software package description.")
received, total = hashlib.sha256(), 0
with urllib.request.urlopen(server + "/software/pi-cctv.bundle", timeout=120) as response, open(path, "wb") as output:
    while True:
        chunk = response.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > size:
            sys.exit("The software package is larger than the dashboard described.")
        received.update(chunk)
        output.write(chunk)
if total != size or received.hexdigest() != digest:
    sys.exit("The downloaded software package failed its integrity check.")
print(commit)
PI_CCTV_PY
)"

  if ! command -v git >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y git
  fi
  g() {{ git -c "safe.directory=$INSTALL_DIR" -C "$INSTALL_DIR" "$@"; }}
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    g fetch -q --no-tags "$bundle" HEAD
    [[ "$(g rev-parse FETCH_HEAD)" == "$commit" ]] || {{ echo "The software package did not contain the dashboard's version." >&2; exit 1; }}
    if ! g merge-base --is-ancestor "$commit" HEAD; then
      g merge -q --ff-only "$commit"
    fi
  elif [[ -e "$INSTALL_DIR" ]]; then
    echo "$INSTALL_DIR exists but is not a Pi CCTV checkout; move it aside and run this again." >&2
    exit 1
  else
    git -c init.defaultBranch=main init -q "$INSTALL_DIR"
    g fetch -q --no-tags "$bundle" HEAD
    [[ "$(g rev-parse FETCH_HEAD)" == "$commit" ]] || {{ echo "The software package did not contain the dashboard's version." >&2; exit 1; }}
    g checkout -q -B main "$commit"
    g remote add origin "$REPO_URL"
  fi
  echo "Installed Pi CCTV software ${{commit:0:12}}."

  if [[ -f /var/lib/pi-cctv/device.json ]]; then
    echo "This Pi is already set up; repairing it with the dashboard's software."
    cd "$INSTALL_DIR"
    python3 -m cctv.update
    return
  fi
  bash "$INSTALL_DIR/install.sh" --discover --skip-download
  echo
  echo "Done. Adopt this Pi on the dashboard under Devices -> Pending devices."
}}

main "$@"
"""
