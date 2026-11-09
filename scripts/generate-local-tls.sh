#!/usr/bin/env bash
set -euo pipefail

IP=""
DNS=""
OUT_DIR=/etc/pi-cctv/tls
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="${2:?--ip requires an address}"; shift 2;;
    --dns) DNS="${2:?--dns requires a hostname}"; shift 2;;
    --out) OUT_DIR="${2:?--out requires a directory}"; shift 2;;
    -h|--help)
      echo "Usage: sudo bash scripts/generate-local-tls.sh --ip SERVER_LAN_IP [--dns SERVER_NAME] [--out /etc/pi-cctv/tls]"
      exit 0;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
[[ -n "$IP" ]] || { echo "--ip is required; use the address the Pis will use for the server." >&2; exit 2; }
python3 - "$IP" "$DNS" <<'PY'
import ipaddress, re, sys
ipaddress.ip_address(sys.argv[1])
if sys.argv[2] and not re.fullmatch(r"[A-Za-z0-9.-]+", sys.argv[2]):
    raise SystemExit("DNS name may contain only letters, digits, dots, and hyphens")
PY
command -v openssl >/dev/null || { echo "Install openssl first." >&2; exit 1; }

install -d -m 0700 "$OUT_DIR"
if [[ ! -e "$OUT_DIR/ca.key" || ! -e "$OUT_DIR/ca.crt" ]]; then
  [[ ! -e "$OUT_DIR/ca.key" && ! -e "$OUT_DIR/ca.crt" ]] || { echo "CA key/certificate pair is incomplete; refusing to overwrite." >&2; exit 1; }
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$OUT_DIR/ca.key"
  chmod 0600 "$OUT_DIR/ca.key"
  openssl req -x509 -new -key "$OUT_DIR/ca.key" -sha256 -days 3650 \
    -subj "/CN=Pi CCTV Local Device CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$OUT_DIR/ca.crt"
  chmod 0644 "$OUT_DIR/ca.crt"
fi

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$OUT_DIR/server.key"
chmod 0600 "$OUT_DIR/server.key"
openssl req -new -key "$OUT_DIR/server.key" -subj "/CN=$IP" -out "$OUT_DIR/server.csr"
{
  cat <<'EOF'
[server_ext]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=@alt_names
[alt_names]
EOF
  printf 'IP.1=%s\n' "$IP"
  if [[ -n "$DNS" ]]; then printf 'DNS.1=%s\n' "$DNS"; fi
} > "$OUT_DIR/server.ext"
openssl x509 -req -in "$OUT_DIR/server.csr" -CA "$OUT_DIR/ca.crt" -CAkey "$OUT_DIR/ca.key" \
  -CAcreateserial -out "$OUT_DIR/server.crt" -days 825 -sha256 -extfile "$OUT_DIR/server.ext" -extensions server_ext
chmod 0644 "$OUT_DIR/server.crt"
rm -f "$OUT_DIR/server.csr" "$OUT_DIR/server.ext" "$OUT_DIR/ca.srl"
echo "Created local CA and HTTPS server certificate in $OUT_DIR. Keep ca.key private."
echo "Install $OUT_DIR/ca.crt on each Pi and trust it before using the HTTPS enrollment URL."
openssl x509 -in "$OUT_DIR/ca.crt" -noout -fingerprint -sha256
