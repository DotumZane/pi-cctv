#!/usr/bin/env bash
# V1 installer for a Raspberry Pi camera or monitor agent.
set -euo pipefail

INSTALL_DIR="${CCTV_INSTALL_DIR:-/opt/pi-cctv}"
REPO_URL="${CCTV_REPO_URL:-https://github.com/DotumZane/pi-cctv.git}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh" >&2
  exit 1
fi

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cat <<EOF
V1 installed at $INSTALL_DIR.

Enroll a camera:
  python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" --server http://SERVER:8080

Enroll a monitor:
  python3 -m cctv.agent --role monitor --name "Control 1" --location "Control Room" --server http://SERVER:8080
EOF
