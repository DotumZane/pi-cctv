#!/usr/bin/env bash
# Optional, reversible setup AP for a wired Raspberry Pi CCTV device.
set -euo pipefail

STATE_DIR=/var/lib/pi-cctv/setup-ap
BRIDGE=pi-cctv-setup-bridge
BRIDGE_PORT=pi-cctv-setup-ethernet-port
NM_CONF=/etc/NetworkManager/conf.d/99-pi-cctv-setup-ap.conf
HOSTAPD_CONF=/etc/hostapd/hostapd.conf
HOSTAPD_DEFAULTS=/etc/default/hostapd
HOSTAPD_OVERRIDE=/etc/systemd/system/hostapd.service.d/10-pi-cctv-unblock-wifi.conf

usage() {
  cat <<'EOF'
Configure or remove the optional Pi CCTV hidden setup access point.

Usage:
  sudo bash scripts/setup-bridged-ap.sh --apply [--ssid SSID] [--country US] [--channel 6]
  sudo bash scripts/setup-bridged-ap.sh --remove
  sudo bash scripts/setup-bridged-ap.sh --status
  sudo bash scripts/setup-bridged-ap.sh --repair

The AP bridges Wi-Fi clients onto the wired LAN. The LAN must provide DHCP,
and its switch port must allow the Pi and connected phone MAC addresses.
The password is requested securely and is not stored in shell history.
EOF
}

MODE=""
SSID="PiCCTV-Setup"
COUNTRY="US"
CHANNEL=6
CONFIG_FILE=""
NONINTERACTIVE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) MODE=apply; shift;;
    --remove) MODE=remove; shift;;
    --status) MODE=status; shift;;
    --repair) MODE=repair; shift;;
    --ssid) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; SSID=$2; shift 2;;
    --country) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; COUNTRY=$2; shift 2;;
    --channel) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; CHANNEL=$2; shift 2;;
    --config-file) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; CONFIG_FILE=$2; shift 2;;
    --noninteractive) NONINTERACTIVE=true; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
[[ -n "$MODE" ]] || { usage >&2; exit 2; }

if [[ -n "$CONFIG_FILE" ]]; then
  [[ -f "$CONFIG_FILE" && ! -L "$CONFIG_FILE" ]] || { echo "AP profile file is missing or is a symlink." >&2; exit 1; }
  mapfile -t PROFILE_VALUES < <(python3 - "$CONFIG_FILE" <<'PY'
import base64, json, sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError) as exc:
    raise SystemExit(f"Invalid AP profile: {exc}")
for key in ("ssid", "password", "country", "channel"):
    item = value.get(key)
    if not isinstance(item, (str, int)):
        raise SystemExit(f"Invalid AP profile field: {key}")
    print(base64.b64encode(str(item).encode()).decode())
PY
  )
  [[ ${#PROFILE_VALUES[@]} -eq 4 ]] || { echo "Could not read complete AP profile." >&2; exit 1; }
  SSID=$(printf '%s' "${PROFILE_VALUES[0]}" | base64 -d)
  AP_PASSWORD=$(printf '%s' "${PROFILE_VALUES[1]}" | base64 -d)
  COUNTRY=$(printf '%s' "${PROFILE_VALUES[2]}" | base64 -d)
  CHANNEL=$(printf '%s' "${PROFILE_VALUES[3]}" | base64 -d)
  unset PROFILE_VALUES
fi

remove_ap() {
  [[ -f "$STATE_DIR/state" ]] || { echo "No saved Pi CCTV AP setup was found."; exit 1; }
  # State values are generated locally as UUIDs and NetworkManager setting values.
  # shellcheck disable=SC1090
  source "$STATE_DIR/state"
  systemctl disable --now hostapd.service >/dev/null 2>&1 || true
  case "$OLD_HOSTAPD_ENABLED" in
    masked) systemctl mask hostapd.service >/dev/null 2>&1 || true;;
    enabled|enabled-runtime) systemctl enable hostapd.service >/dev/null 2>&1 || true;;
    *) systemctl disable hostapd.service >/dev/null 2>&1 || true;;
  esac
  rm -f "$HOSTAPD_CONF" "$NM_CONF"
  rm -f "$HOSTAPD_OVERRIDE"
  rmdir /etc/systemd/system/hostapd.service.d 2>/dev/null || true
  systemctl daemon-reload
  if [[ -f "$STATE_DIR/hostapd-defaults" ]]; then
    install -m 0644 "$STATE_DIR/hostapd-defaults" "$HOSTAPD_DEFAULTS"
  else
    rm -f "$HOSTAPD_DEFAULTS"
  fi
  if [[ -n "${PORT_UUID:-}" ]]; then
    if nmcli -t -f UUID connection show | grep -Fxq "$PORT_UUID"; then
      nmcli connection down uuid "$PORT_UUID" >/dev/null 2>&1 || true
      nmcli connection delete uuid "$PORT_UUID" >/dev/null
    fi
  fi
  if nmcli -t -f NAME connection show | grep -Fxq "$BRIDGE"; then
    nmcli connection down "$BRIDGE" >/dev/null 2>&1 || true
    nmcli connection delete "$BRIDGE" >/dev/null
  fi
  if [[ -n "${PORT_UUID:-}" ]]; then
    nmcli connection modify uuid "$ETH_UUID" connection.autoconnect "$OLD_AUTOCONNECT"
  else
    # Older installs modified the original Ethernet profile in place.
    nmcli connection modify uuid "$ETH_UUID" \
      connection.master "$OLD_MASTER" connection.port-type "$OLD_PORT_TYPE"
    nmcli connection modify uuid "$ETH_UUID" \
      ipv4.method "$OLD_IPV4" ipv6.method "$OLD_IPV6" \
      connection.autoconnect "$OLD_AUTOCONNECT"
  fi
  nmcli general reload
  nmcli device set wlan0 managed yes
  nmcli connection reload
  nmcli connection up uuid "$ETH_UUID" ifname eth0 || true
  rm -f "$STATE_DIR/state" "$STATE_DIR/hostapd-defaults"
  rmdir "$STATE_DIR" 2>/dev/null || true
  echo "Removed the Pi CCTV setup AP and restored the previous Ethernet profile."
}

ap_is_healthy() {
  [[ -f "$STATE_DIR/state" && -s "$HOSTAPD_CONF" ]] || return 1
  systemctl is-active --quiet hostapd.service || return 1
  bridge link show dev eth0 | grep -q 'master br0' || return 1
  bridge link show dev wlan0 | grep -q 'master br0' || return 1
  ip -4 addr show dev br0 | grep -q 'inet ' || return 1
}

if [[ "$MODE" == remove ]]; then
  remove_ap
  exit 0
fi
if [[ "$MODE" == status ]]; then
  if ap_is_healthy; then
    echo "Setup AP is active and both Wi-Fi and Ethernet are attached to br0."
    exit 0
  fi
  echo "Setup AP bridge is not healthy: expected hostapd active, wlan0 and eth0 on br0, and a LAN address on br0." >&2
  exit 1
fi
if [[ "$MODE" == repair ]]; then
  PROFILE_PATH=/var/lib/pi-cctv/setup-ap-profile.json
  [[ -f "$PROFILE_PATH" ]] || { echo "No server-managed setup Wi-Fi profile is saved on this Pi." >&2; exit 1; }
  if [[ -f "$STATE_DIR/state" ]]; then
    bash "$0" --remove
  fi
  exec bash "$0" --apply --config-file "$PROFILE_PATH" --noninteractive
fi

[[ ! -e "$STATE_DIR/state" ]] || { echo "Setup AP is already configured; use --remove first." >&2; exit 1; }
[[ ! -e "$HOSTAPD_CONF" ]] || { echo "$HOSTAPD_CONF already exists; refusing to overwrite it." >&2; exit 1; }
[[ ! -e "$NM_CONF" ]] || { echo "$NM_CONF already exists; refusing to overwrite it." >&2; exit 1; }
if nmcli -t -f NAME connection show | grep -Fxq "$BRIDGE" || \
   nmcli -t -f NAME connection show | grep -Fxq "$BRIDGE_PORT"; then
  echo "A setup bridge profile already exists without saved state; refusing to overwrite it." >&2
  exit 1
fi
[[ "$SSID" =~ ^[[:print:]]{1,32}$ ]] || { echo "SSID must be 1–32 printable characters." >&2; exit 2; }
[[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || { echo "Country must be a two-letter uppercase code, e.g. US." >&2; exit 2; }
[[ "$CHANNEL" =~ ^([1-9]|1[01])$ ]] || { echo "Use a 2.4 GHz channel from 1 to 11." >&2; exit 2; }
command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) is required." >&2; exit 1; }
[[ -e /sys/class/net/eth0 && -e /sys/class/net/wlan0 ]] || { echo "This setup requires built-in eth0 and wlan0 interfaces." >&2; exit 1; }
if [[ -z "$CONFIG_FILE" ]]; then
  [[ -r /dev/tty ]] || { echo "Run this command from an interactive terminal so the shared password can be entered securely." >&2; exit 1; }
  read -r -s -p "Shared Wi-Fi password (8–63 characters): " AP_PASSWORD </dev/tty
  printf '\n'
fi
[[ "$AP_PASSWORD" =~ ^[[:print:]]{8,63}$ ]] || { echo "Password must be 8–63 printable characters." >&2; exit 2; }

ETH_UUID=$(nmcli -g GENERAL.CON-UUID device show eth0 | head -n1)
[[ "$ETH_UUID" =~ ^[[:xdigit:]-]{36}$ ]] || { echo "Could not identify the active NetworkManager Ethernet profile on eth0." >&2; exit 1; }
ETH_MAC=$(cat /sys/class/net/eth0/address)
[[ "$ETH_MAC" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || { echo "Could not read eth0 MAC address." >&2; exit 1; }
OLD_MASTER=$(nmcli -g connection.master connection show uuid "$ETH_UUID")
OLD_PORT_TYPE=$(nmcli -g connection.port-type connection show uuid "$ETH_UUID")
OLD_IPV4=$(nmcli -g ipv4.method connection show uuid "$ETH_UUID")
OLD_IPV6=$(nmcli -g ipv6.method connection show uuid "$ETH_UUID")
OLD_AUTOCONNECT=$(nmcli -g connection.autoconnect connection show uuid "$ETH_UUID")
OLD_HOSTAPD_ENABLED=$(systemctl is-enabled hostapd.service 2>/dev/null || true)
[[ "$OLD_MASTER" == "--" ]] && OLD_MASTER=""
[[ "$OLD_PORT_TYPE" == "--" ]] && OLD_PORT_TYPE=""

if [[ "$NONINTERACTIVE" != true ]]; then
  echo "This will bridge the hidden SSID '$SSID' to eth0 and replace the Pi's active Ethernet profile with a bridge profile. The DHCP server on your wired LAN will assign addresses to both the Pi and your phone."
  echo "The Pi may briefly disconnect while networking restarts. It will not provide DHCP, NAT, or internet routing."
  read -r -p "Type APPLY to continue: " CONFIRM </dev/tty
  [[ "$CONFIRM" == APPLY ]] || { echo "No changes made."; exit 1; }
fi

install -d -m 0700 "$STATE_DIR"
printf 'ETH_UUID=%q\nOLD_MASTER=%q\nOLD_PORT_TYPE=%q\nOLD_IPV4=%q\nOLD_IPV6=%q\nOLD_AUTOCONNECT=%q\n' \
  "$ETH_UUID" "$OLD_MASTER" "$OLD_PORT_TYPE" "$OLD_IPV4" "$OLD_IPV6" "$OLD_AUTOCONNECT" > "$STATE_DIR/state"
chmod 0600 "$STATE_DIR/state"
printf 'OLD_HOSTAPD_ENABLED=%q\n' "$OLD_HOSTAPD_ENABLED" >> "$STATE_DIR/state"
printf 'PORT_UUID=\n' >> "$STATE_DIR/state"
if [[ -f "$HOSTAPD_DEFAULTS" ]]; then
  cp -p "$HOSTAPD_DEFAULTS" "$STATE_DIR/hostapd-defaults"
fi

if ! command -v hostapd >/dev/null || ! command -v rfkill >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y hostapd rfkill
fi

install -d -m 0755 /etc/NetworkManager/conf.d /etc/hostapd
install -d -m 0755 /etc/systemd/system/hostapd.service.d
RFKILL_BIN=$(command -v rfkill || true)
[[ -n "$RFKILL_BIN" ]] || { echo "rfkill is required to enable the Pi's Wi-Fi radio." >&2; exit 1; }
cat > "$HOSTAPD_OVERRIDE" <<EOF
[Service]
ExecStartPre=$RFKILL_BIN unblock wlan
EOF
chmod 0644 "$HOSTAPD_OVERRIDE"
systemctl daemon-reload
cat > "$NM_CONF" <<'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
chmod 0644 "$NM_CONF"
nmcli general reload
nmcli device set wlan0 managed no

cat > "$HOSTAPD_CONF" <<EOF
country_code=$COUNTRY
interface=wlan0
bridge=br0
driver=nl80211
ssid=$SSID
ignore_broadcast_ssid=1
hw_mode=g
channel=$CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$AP_PASSWORD
EOF
chmod 0600 "$HOSTAPD_CONF"
unset AP_PASSWORD

if [[ -f "$HOSTAPD_DEFAULTS" ]]; then
  if grep -q '^DAEMON_CONF=' "$HOSTAPD_DEFAULTS"; then
    sed -i 's|^DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' "$HOSTAPD_DEFAULTS"
  else
    printf '\nDAEMON_CONF="/etc/hostapd/hostapd.conf"\n' >> "$HOSTAPD_DEFAULTS"
  fi
else
  printf 'DAEMON_CONF="/etc/hostapd/hostapd.conf"\n' > "$HOSTAPD_DEFAULTS"
fi

nmcli connection add type bridge con-name "$BRIDGE" ifname br0 \
  connection.autoconnect-ports 1 bridge.stp no bridge.mac-address "$ETH_MAC" \
  ipv4.method auto ipv6.method auto >/dev/null
BRIDGE_UUID=$(nmcli -g connection.uuid connection show "$BRIDGE")
nmcli connection modify uuid "$ETH_UUID" connection.autoconnect no
nmcli connection add type ethernet con-name "$BRIDGE_PORT" ifname eth0 \
  connection.master "$BRIDGE_UUID" connection.port-type bridge \
  ipv4.method disabled ipv6.method disabled connection.autoconnect yes >/dev/null
PORT_UUID=$(nmcli -g connection.uuid connection show "$BRIDGE_PORT")
printf 'PORT_UUID=%q\n' "$PORT_UUID" >> "$STATE_DIR/state"
nmcli connection reload
nmcli connection down uuid "$ETH_UUID" >/dev/null 2>&1 || true
nmcli connection up "$BRIDGE"
nmcli connection up uuid "$PORT_UUID" ifname eth0
nmcli connection up "$BRIDGE"

if ! bridge link show dev eth0 | grep -q 'master br0'; then
  echo "Ethernet did not attach to the setup bridge; restoring the original wired profile." >&2
  remove_ap
  exit 1
fi
if ! ip -4 addr show dev br0 | grep -q 'inet '; then
  echo "The setup bridge did not receive an IPv4 address from the wired LAN; restoring the original wired profile." >&2
  remove_ap
  exit 1
fi

"$RFKILL_BIN" unblock wlan
systemctl unmask hostapd.service >/dev/null 2>&1 || true
if ! systemctl enable --now hostapd.service; then
  echo "hostapd failed to start. Recent service logs:" >&2
  journalctl -u hostapd.service -n 40 --no-pager >&2 || true
  echo "Restoring the original wired network profile." >&2
  remove_ap
  exit 1
fi
echo "Setup AP enabled. Connect your phone to the hidden '$SSID' network using the shared password. The phone should receive an address from the wired LAN and reach the dashboard at its usual server address."
echo "To undo this on the Pi: sudo bash /opt/pi-cctv/scripts/setup-bridged-ap.sh --remove"
