#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir=/var/lib/pi-cctv-host-update
config=/etc/pi-cctv-server-update.json
repo_owner="$(stat -c '%U' "$repo_dir")"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose must be installed before enabling dashboard server updates." >&2
  exit 1
fi
if ! getent group docker >/dev/null; then
  echo "The docker group is missing; install Docker Engine first." >&2
  exit 1
fi
if [[ ! -f "$repo_dir/docker-compose.yml" || ! -d "$repo_dir/.git" ]]; then
  echo "Run this script from a Pi CCTV Git checkout." >&2
  exit 1
fi
if [[ "$repo_owner" == root || ! "$repo_owner" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "The project checkout must be owned by a regular Linux user." >&2
  exit 1
fi
if [[ "$repo_dir" =~ [[:space:]] ]]; then
  echo "The server checkout path cannot contain whitespace." >&2
  exit 1
fi

install -d -o root -g docker -m 2770 "$state_dir"
printf '{"project_dir":"%s"}\n' "$repo_dir" > "$config"
chmod 0644 "$config"
install -o root -g docker -m 0660 /dev/null "$state_dir/installed"
install -o root -g docker -m 0660 /dev/null "$state_dir/status.json"
install -o root -g docker -m 0660 /dev/null "$state_dir/os-installed"
install -o root -g docker -m 0660 /dev/null "$state_dir/os-status.json"
sed -e "s|@CCTVOPS_USER@|$repo_owner|" -e "s|@CCTVOPS_REPO@|$repo_dir|" \
  "$repo_dir/services/pi-cctv-server-update.service" > /etc/systemd/system/pi-cctv-server-update.service
chown root:root /etc/systemd/system/pi-cctv-server-update.service
chmod 0644 /etc/systemd/system/pi-cctv-server-update.service
install -o root -g root -m 0644 "$repo_dir/services/pi-cctv-server-update.path" /etc/systemd/system/pi-cctv-server-update.path
sed -e "s|@CCTVOPS_REPO@|$repo_dir|" \
  "$repo_dir/services/pi-cctv-server-os-update.service" > /etc/systemd/system/pi-cctv-server-os-update.service
chown root:root /etc/systemd/system/pi-cctv-server-os-update.service
chmod 0644 /etc/systemd/system/pi-cctv-server-os-update.service
install -o root -g root -m 0644 "$repo_dir/services/pi-cctv-server-os-update.path" /etc/systemd/system/pi-cctv-server-os-update.path
systemctl daemon-reload
systemctl enable --now pi-cctv-server-update.path
systemctl enable --now pi-cctv-server-os-update.path
echo "Dashboard server software and Ubuntu OS updates are installed for checkout: $repo_dir"
