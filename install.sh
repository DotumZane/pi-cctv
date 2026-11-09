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
ENROLL="false"
BOOTSTRAP_TOKEN=""
SETUP_AP="false"
AP_SSID="PiCCTV-Setup"
AP_COUNTRY="US"
AP_CHANNEL="6"
SERVER_CA=""

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
    --enroll) ENROLL="true"; shift;;
    --bootstrap-token) BOOTSTRAP_TOKEN="$2"; shift 2;;
    --setup-ap) SETUP_AP="true"; shift;;
    --ap-ssid) AP_SSID="$2"; shift 2;;
    --ap-country) AP_COUNTRY="$2"; shift 2;;
    --ap-channel) AP_CHANNEL="$2"; shift 2;;
    --server-ca) SERVER_CA="$2"; shift 2;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

if [[ "$SKIP_PACKAGES" != "true" && -x "$(command -v apt-get 2>/dev/null || true)" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y \
    git python3 \
    gstreamer1.0-tools gstreamer1.0-libcamera gstreamer1.0-rtsp \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav
fi

configure_imx477() {
  local config
  for config in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$config" ]]; then
      if ! grep -Eiq '^[[:space:]]*dtoverlay[[:space:]]*=[[:space:]]*imx477' "$config"; then
        printf '\n# Pi CCTV: Arducam/Raspberry Pi HQ IMX477 camera\ndtoverlay=imx477\n' >> "$config"
        echo "Enabled IMX477 camera overlay in $config; reboot may be required."
      fi
      return
    fi
  done
  echo "Warning: could not find the Pi camera config file; enable dtoverlay=imx477 before using an IMX477 camera." >&2
}

if [[ "$ROLE" == "camera" ]]; then
  configure_imx477
fi

if [[ -n "$SERVER_CA" ]]; then
  [[ -f "$SERVER_CA" ]] || { echo "Local server CA certificate not found: $SERVER_CA" >&2; exit 1; }
  install -m 0644 "$SERVER_CA" /usr/local/share/ca-certificates/pi-cctv-server-ca.crt
  update-ca-certificates
fi

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -c "safe.directory=$INSTALL_DIR" -C "$INSTALL_DIR" pull --ff-only origin main
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

configure_setup_ap() {
  [[ "$SETUP_AP" == "true" ]] || return 0
  [[ -x "$INSTALL_DIR/scripts/setup-bridged-ap.sh" ]] || chmod 0755 "$INSTALL_DIR/scripts/setup-bridged-ap.sh"
  bash "$INSTALL_DIR/scripts/setup-bridged-ap.sh" --apply \
    --ssid "$AP_SSID" --country "$AP_COUNTRY" --channel "$AP_CHANNEL"
}

if [[ "$ENROLL" == "true" || -n "$ROLE" ]]; then
  if [[ "$ENROLL" == "true" ]]; then
    [[ -n "$SERVER" && -n "$MEDIA_SERVER" ]] || { echo "--enroll requires --server and --media-server" >&2; exit 2; }
    if [[ ! -f /var/lib/pi-cctv/device.json && -z "$BOOTSTRAP_TOKEN" ]]; then
      echo "First-time --enroll requires --bootstrap-token." >&2
      exit 2
    fi
    AGENT_ARGS="--enroll --server $(printf '%q' "$SERVER") --media-server $(printf '%q' "$MEDIA_SERVER")"
    [[ -z "$BOOTSTRAP_TOKEN" ]] || AGENT_ARGS+=" --bootstrap-token $(printf '%q' "$BOOTSTRAP_TOKEN")"
  else
    [[ "$ROLE" == "camera" || "$ROLE" == "monitor" ]] || { echo "--role must be camera or monitor" >&2; exit 2; }
    [[ -n "$NAME" && -n "$SERVER" && -n "$MEDIA_SERVER" ]] || { echo "--role requires --name, --server, and --media-server" >&2; exit 2; }
    AGENT_ARGS="--role $(printf '%q' "$ROLE") --name $(printf '%q' "$NAME") --location $(printf '%q' "$LOCATION") --server $(printf '%q' "$SERVER") --media-server $(printf '%q' "$MEDIA_SERVER") --token $(printf '%q' "$TOKEN") --run-media"
  fi
  install -m 0644 "$INSTALL_DIR/services/pi-cctv-agent.service" /etc/systemd/system/pi-cctv-agent.service
  umask 077
  printf '%s\n' "CCTV_AGENT_ARGS=$AGENT_ARGS" > /etc/default/pi-cctv-agent
  systemctl daemon-reload
  systemctl enable --now pi-cctv-agent.service
  echo "Installed and started pi-cctv-agent.service"
  configure_setup_ap
  exit 0
fi

configure_setup_ap

cat <<EOF
V1 installed at $INSTALL_DIR.

Enroll a camera:
  cd $INSTALL_DIR && python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" --server http://SERVER:8080

Enroll a monitor:
  cd $INSTALL_DIR && python3 -m cctv.agent --role monitor --name "Control 1" --location "Control Room" --server http://SERVER:8080
EOF
