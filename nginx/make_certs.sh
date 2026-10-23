#!/usr/bin/env bash
# Create a local certificate authority (once) and a TLS certificate for the gateway.
# Run on the host from the project folder:
#   ./nginx/make_certs.sh
#
# - The CA lives in ~/.local-ca (mode 700), OUTSIDE this project. Its private key
#   can sign certificates your machine will trust, so never copy it anywhere.
# - The gateway certificate goes to nginx/certs/ and covers:
#   langfuse, grafana, api, llm and code .ai.internal, plus localhost
# - Re-running keeps the CA and issues a fresh gateway certificate (valid 825 days).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CA_DIR="${CA_DIR:-$HOME/.local-ca}"
OUT="$ROOT/nginx/certs"
NAMES=(langfuse.ai.internal grafana.ai.internal api.ai.internal llm.ai.internal code.ai.internal localhost)

command -v openssl >/dev/null || { echo "[!] openssl is not installed (sudo apt install openssl)" >&2; exit 1; }
umask 077
mkdir -p "$CA_DIR" "$OUT"
chmod 700 "$CA_DIR"

if [ ! -f "$CA_DIR/ca.key" ]; then
  openssl genrsa -out "$CA_DIR/ca.key" 4096 2>/dev/null
  openssl req -x509 -new -key "$CA_DIR/ca.key" -sha256 -days 3650 \
    -subj "/CN=Enterprise AI Stack Local CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$CA_DIR/ca.crt"
  chmod 600 "$CA_DIR/ca.key"; chmod 644 "$CA_DIR/ca.crt"
  echo "[*] Created local CA in $CA_DIR (ca.crt is what you trust; ca.key stays private)"
else
  echo "[*] Using existing local CA in $CA_DIR"
fi

SAN="$(printf 'DNS:%s,' "${NAMES[@]}")IP:127.0.0.1"
EXT="$(mktemp)"; CSR="$(mktemp)"
trap 'rm -f "$EXT" "$CSR"' EXIT
cat > "$EXT" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
EOF

openssl req -new -newkey rsa:2048 -nodes -keyout "$OUT/ai.internal.key" \
  -subj "/CN=ai.internal" -out "$CSR" 2>/dev/null
openssl x509 -req -in "$CSR" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
  -days 825 -sha256 -extfile "$EXT" -out "$OUT/ai.internal.crt" 2>/dev/null
chmod 600 "$OUT/ai.internal.key"; chmod 644 "$OUT/ai.internal.crt"; chmod 755 "$OUT"

openssl verify -CAfile "$CA_DIR/ca.crt" "$OUT/ai.internal.crt" >/dev/null
echo "[*] Gateway certificate: $OUT/ai.internal.crt (valid 825 days)"
echo "    covers: ${NAMES[*]}"
echo
echo "Trust the CA once (see the run order): $CA_DIR/ca.crt"
