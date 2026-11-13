#!/usr/bin/env bash
# Install the RSG early boot image on monitor Pis only. This runs outside the
# agent service so an APT/initramfs refresh cannot delay live monitor output.
set -euo pipefail

INSTALL_DIR="${CCTV_INSTALL_DIR:-/opt/pi-cctv}"
IMAGE_DIR="$INSTALL_DIR/assets"
# Raspberry Pi's fullscreen_logo loader requires an uncompressed 24-bit TGA
# with no more than 224 colours. Select a pre-rendered canvas for the active
# HDMI mode because the kernel splash loader does not scale images.
STATE_DIR=/var/lib/pi-cctv
STAMP="$STATE_DIR/monitor-splash.sha256"

[[ "$(id -u)" -eq 0 ]] || { echo "Monitor splash setup requires root" >&2; exit 1; }
[[ -f /boot/firmware/cmdline.txt ]] || { echo "Monitor splash requires Raspberry Pi OS boot firmware" >&2; exit 1; }
[[ -d "$IMAGE_DIR" ]] || { echo "Monitor splash assets missing: $IMAGE_DIR" >&2; exit 1; }

mode="$(find /sys/class/drm -type f -name mode -print -quit 2>/dev/null | xargs -r cat 2>/dev/null | head -n 1)"
case "$mode" in
  800x600) IMAGE="$IMAGE_DIR/rsg-monitor-splash-800x600.tga" ;;
  1024x768) IMAGE="$IMAGE_DIR/rsg-monitor-splash-1024x768.tga" ;;
  1280x720) IMAGE="$IMAGE_DIR/rsg-monitor-splash-1280x720.tga" ;;
  1920x1080) IMAGE="$IMAGE_DIR/rsg-monitor-splash-1920x1080.tga" ;;
  *x*)
    width="${mode%x*}"
    height="${mode#*x}"
    if [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ && $((width * 10)) -ge $((height * 15)) ]]; then
      IMAGE="$IMAGE_DIR/rsg-monitor-splash-1280x720.tga"
    else
      IMAGE="$IMAGE_DIR/rsg-monitor-splash-1024x768.tga"
    fi
    ;;
  *) IMAGE="$IMAGE_DIR/rsg-monitor-splash-1024x768.tga" ;;
esac
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
