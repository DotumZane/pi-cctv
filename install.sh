#!/usr/bin/env bash
# V1 installer for a Raspberry Pi camera or monitor agent.
set -euo pipefail

INSTALL_DIR="${CCTV_INSTALL_DIR:-/opt/pi-cctv}"
REPO_URL="${CCTV_REPO_URL:-https://github.com/DotumZane/pi-cctv.git}"
ROLE=""
NAME=""
LOCATION=""
SERVER=""
MEDIA_SERVER=""
TOKEN=""
SKIP_PACKAGES="false"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh" >&2
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --location) LOCATION="$2"; shift 2;;
    --server) SERVER="$2"; shift 2;;
    --media-server) MEDIA_SERVER="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --skip-packages) SKIP_PACKAGES="true"; shift;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

if [[ "$SKIP_PACKAGES" != "true" && -x "$(command -v apt-get 2>/dev/null || true)" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y \
    git python3 \
    gstreamer1.0-tools gstreamer1.0-libcamera \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav
fi

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

if [[ -n "$ROLE" ]]; then
  [[ "$ROLE" == "camera" || "$ROLE" == "monitor" ]] || { echo "--role must be camera or monitor" >&2; exit 2; }
  [[ -n "$NAME" && -n "$SERVER" && -n "$MEDIA_SERVER" ]] || { echo "--role requires --name, --server, and --media-server" >&2; exit 2; }
  install -m 0644 "$INSTALL_DIR/services/pi-cctv-agent.service" /etc/systemd/system/pi-cctv-agent.service
  umask 077
  printf '%s\n' "CCTV_AGENT_ARGS=--role $(printf '%q' "$ROLE") --name $(printf '%q' "$NAME") --location $(printf '%q' "$LOCATION") --server $(printf '%q' "$SERVER") --media-server $(printf '%q' "$MEDIA_SERVER") --token $(printf '%q' "$TOKEN") --run-media" > /etc/default/pi-cctv-agent
  systemctl daemon-reload
  systemctl enable --now pi-cctv-agent.service
  echo "Installed and started pi-cctv-agent.service"
  exit 0
fi

cat <<EOF
V1 installed at $INSTALL_DIR.

Enroll a camera:
  cd $INSTALL_DIR && python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" --server http://SERVER:8080

Enroll a monitor:
  cd $INSTALL_DIR && python3 -m cctv.agent --role monitor --name "Control 1" --location "Control Room" --server http://SERVER:8080
EOF
