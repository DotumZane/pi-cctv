#!/usr/bin/env bash
# Install the RSG early boot image on monitor Pis only. This runs outside the
# agent service so an APT/initramfs refresh cannot delay live monitor output.
set -euo pipefail

INSTALL_DIR="${CCTV_INSTALL_DIR:-/opt/pi-cctv}"
# The kernel fullscreen_logo path is unreliable across the monitor hardware
# fleet. Keep the boot path clean; adaptive splash rendering belongs in the
# user-space monitor runtime, after DRM has detected the active display mode.
CMDLINE_FILENAME=/boot/firmware/cmdline.txt
STATE_DIR=/var/lib/pi-cctv
STAMP="$STATE_DIR/monitor-splash.sha256"

[[ "$(id -u)" -eq 0 ]] || { echo "Monitor splash setup requires root" >&2; exit 1; }
[[ -f /boot/firmware/cmdline.txt ]] || { echo "Monitor splash requires Raspberry Pi OS boot firmware" >&2; exit 1; }
[[ -f "$CMDLINE_FILENAME" ]] || { echo "Monitor boot command line missing: $CMDLINE_FILENAME" >&2; exit 1; }

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/monitor-splash.lock"
flock 9
fingerprint="$(sha256sum "$0" | sha256sum | cut -d ' ' -f 1)"
if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$fingerprint" ]]; then
  exit 0
fi

if [[ ! -e "${CMDLINE_FILENAME}.before-kernel-splash" ]]; then
  cp "$CMDLINE_FILENAME" "${CMDLINE_FILENAME}.before-kernel-splash"
fi
cmdline="$(tr '\r\n' ' ' < "$CMDLINE_FILENAME")"
for key in fullscreen_logo fullscreen_logo_name vt.global_cursor_default; do
  cmdline="$(echo "$cmdline" | sed -E "s/(^| )${key}=[^ ]*//g")"
done
cmdline="$(echo "$cmdline" | tr -s ' ' | sed -E 's/^ +| +$//g')"
printf '%s\n' "$cmdline" > "$CMDLINE_FILENAME"
printf '%s\n' "$fingerprint" > "$STAMP"
echo "Kernel monitor splash disabled; adaptive user-space display startup will handle the monitor."
