#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir=/var/lib/pi-cctv-host-update
config=/etc/pi-cctv-server-update.json
repo_owner="$(stat -c '%U' "$repo_dir")"
branch=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) branch="${2:-}"; shift 2;;
    *) echo "Unknown option: $1 (supported: --branch NAME)" >&2; exit 2;;
  esac
done

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

current_branch="$(runuser -u "$repo_owner" -- git -C "$repo_dir" branch --show-current)"
if [[ -z "$branch" ]]; then
  branch="$current_branch"
fi
if [[ ! "$branch" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ || "$branch" == *..* ]]; then
  echo "The update branch must be a plain Git branch name, for example main or stable." >&2
  exit 1
fi
if [[ "$current_branch" != "$branch" ]]; then
  echo "The server checkout is on '${current_branch:-a detached commit}', not '$branch'." >&2
  echo "Switch it first as $repo_owner: git -C $repo_dir checkout $branch" >&2
  exit 1
fi

install -d -o root -g docker -m 2770 "$state_dir"
install -d -o root -g docker -m 2770 "$state_dir/software"
printf '{"project_dir":"%s","branch":"%s"}\n' "$repo_dir" "$branch" > "$config"
chmod 0644 "$config"
install -o root -g docker -m 0660 /dev/null "$state_dir/installed"
install -o root -g docker -m 0660 /dev/null "$state_dir/status.json"
install -o root -g docker -m 0660 /dev/null "$state_dir/os-installed"
install -o root -g docker -m 0660 /dev/null "$state_dir/os-status.json"
install -o root -g docker -m 0660 /dev/null "$state_dir/reboot-installed"
install -o root -g docker -m 0660 /dev/null "$state_dir/reboot-status.json"
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
sed -e "s|@CCTVOPS_REPO@|$repo_dir|" \
  "$repo_dir/services/pi-cctv-server-reboot.service" > /etc/systemd/system/pi-cctv-server-reboot.service
chown root:root /etc/systemd/system/pi-cctv-server-reboot.service
chmod 0644 /etc/systemd/system/pi-cctv-server-reboot.service
install -o root -g root -m 0644 "$repo_dir/services/pi-cctv-server-reboot.path" /etc/systemd/system/pi-cctv-server-reboot.path
sed -e "s|@CCTVOPS_REPO@|$repo_dir|" \
  "$repo_dir/services/pi-cctv-server-reboot-status.service" > /etc/systemd/system/pi-cctv-server-reboot-status.service
chown root:root /etc/systemd/system/pi-cctv-server-reboot-status.service
chmod 0644 /etc/systemd/system/pi-cctv-server-reboot-status.service
systemctl daemon-reload
systemctl enable --now pi-cctv-server-update.path
systemctl enable --now pi-cctv-server-os-update.path
systemctl enable --now pi-cctv-server-reboot.path
systemctl enable pi-cctv-server-reboot-status.service
if ! runuser -u "$repo_owner" -g docker -- /usr/bin/python3 "$repo_dir/scripts/pi-cctv-server-update.py" --publish-software; then
  echo "Warning: Pi software could not be packaged yet; Pis will use GitHub until the next dashboard server update." >&2
fi
echo "Dashboard server software, Ubuntu OS updates, and reboot controls are installed for checkout: $repo_dir (branch $branch)"
