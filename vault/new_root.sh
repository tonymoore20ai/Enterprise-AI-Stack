#!/usr/bin/env bash
# Generate a fresh, temporary root token from the unseal key in ~/.vault-unseal.
# Use it for admin work (e.g. ./vault/bootstrap.sh), then revoke it again:
#   ./vault/new_root.sh
#   ./vault/bootstrap.sh
#   (revoke - command is printed at the end of this script)
set -euo pipefail

CONTAINER="${VAULT_CONTAINER:-enterprise-vault}"
KEYFILE="${VAULT_UNSEAL_FILE:-$HOME/.vault-unseal}"

[ -r "$KEYFILE" ] || { echo "[!] No unseal key at $KEYFILE" >&2; exit 1; }
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(d[k] for k in sys.argv[1:] if d.get(k)))' "$@"; }

# Cancel any half-finished attempt, then start a new one
docker exec "$CONTAINER" vault operator generate-root -cancel > /dev/null 2>&1 || true
init="$(docker exec "$CONTAINER" vault operator generate-root -init -format=json)"
nonce="$(field nonce <<<"$init")"
otp="$(field otp <<<"$init")"

# Submit the unseal key on stdin (read inside the container, never on the command line)
result="$(docker exec -i -e NONCE="$nonce" "$CONTAINER" \
  sh -c 'read -r key && exec vault operator generate-root -format=json -nonce="$NONCE" "$key"' < "$KEYFILE")"
encoded="$(field encoded_token encoded_root_token <<<"$result")"

token="$(docker exec "$CONTAINER" vault operator generate-root -decode="$encoded" -otp="$otp")"
unset init result encoded otp

echo
echo "    TEMPORARY ROOT TOKEN: $token"
echo
echo "    Use it for ./vault/bootstrap.sh, then revoke it:"
echo '    read -rsp "Root token: " VAULT_TOKEN; echo; export VAULT_TOKEN'
echo '    docker exec -e VAULT_TOKEN enterprise-vault vault token revoke -self; unset VAULT_TOKEN'
unset token
