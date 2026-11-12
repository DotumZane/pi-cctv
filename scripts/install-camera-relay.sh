#!/usr/bin/env bash
set -euo pipefail

# Pinned official MediaMTX release; verify the archive before installing it.
version=1.21.3
case "$(uname -m)" in
  aarch64) arch=arm64; checksum=27f038237cb5f77a724f6afc58eca2798f2bc4edf9989895631a80eeb41a4fe2 ;;
  armv7l) arch=armv7; checksum=134b53bfdb26583d02e1c27fc256d587cf3482e80c9e3b49083595e19a096284 ;;
  armv6l) arch=armv6; checksum=cbacd828dd7c8bb67954eee2b0b5590b1f1f6994faa6fcd86a3cf0ba50213095 ;;
  *) echo "Unsupported Pi architecture for local camera relay" >&2; exit 1 ;;
esac

if [[ ! -x /usr/local/bin/pi-cctv-mediamtx || ! -f /etc/pi-cctv/relay-version || "$(cat /etc/pi-cctv/relay-version)" != "$version-$arch" ]]; then
  archive="mediamtx_v${version}_linux_${arch}.tar.gz"
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  python3 - "$version" "$archive" "$temp_dir" <<'PY'
import pathlib
import sys
import urllib.request
version, archive, directory = sys.argv[1:]
url = f"https://github.com/bluenviron/mediamtx/releases/download/v{version}/{archive}"
with urllib.request.urlopen(url, timeout=60) as response:
    pathlib.Path(directory, archive).write_bytes(response.read())
PY
  printf '%s  %s\n' "$checksum" "$temp_dir/$archive" | sha256sum --check --status
  tar -xzf "$temp_dir/$archive" -C "$temp_dir" mediamtx
  install -m 0755 "$temp_dir/mediamtx" /usr/local/bin/pi-cctv-mediamtx
  install -d -m 0755 /etc/pi-cctv
  printf '%s\n' "$version-$arch" > /etc/pi-cctv/relay-version
fi
install -d -m 0755 /etc/pi-cctv
source_dir="${CCTV_INSTALL_DIR:-/opt/pi-cctv}"
install -m 0644 "$source_dir/services/pi-cctv-relay.yml" /etc/pi-cctv/relay.yml
install -m 0644 "$source_dir/services/pi-cctv-relay.service" /etc/systemd/system/pi-cctv-relay.service
systemctl daemon-reload
