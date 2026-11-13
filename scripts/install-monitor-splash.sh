#!/usr/bin/env bash
# Install the RSG early boot image on monitor Pis only. This runs outside the
# agent service so an APT/initramfs refresh cannot delay live monitor output.
set -euo pipefail

INSTALL_DIR="${CCTV_INSTALL_DIR:-/opt/pi-cctv}"
IMAGE="$INSTALL_DIR/assets/rsg-monitor-splash.tga"
# Raspberry Pi's fullscreen_logo loader requires an uncompressed 24-bit TGA
# with no more than 224 colours; keep this validation in the packaged asset
# and let configure-splash perform the platform-side checks.
STATE_DIR=/var/lib/pi-cctv
STAMP="$STATE_DIR/monitor-splash.sha256"

[[ "$(id -u)" -eq 0 ]] || { echo "Monitor splash setup requires root" >&2; exit 1; }
[[ -f /boot/firmware/cmdline.txt ]] || { echo "Monitor splash requires Raspberry Pi OS boot firmware" >&2; exit 1; }
[[ -f "$IMAGE" ]] || { echo "Monitor splash image missing: $IMAGE" >&2; exit 1; }

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/monitor-splash.lock"
flock 9
fingerprint="$(sha256sum "$IMAGE" "$0" | sha256sum | cut -d ' ' -f 1)"
if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$fingerprint" ]]; then
  exit 0
fi

if ! command -v configure-splash >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y rpi-splash-screen-support
fi
configure-splash "$IMAGE"
printf '%s\n' "$fingerprint" > "$STAMP"
echo "RSG monitor boot splash installed; it will appear on the next boot."
