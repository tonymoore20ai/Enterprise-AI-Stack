#!/usr/bin/env bash
# One-time Vault initialisation. Run on the host from the project folder:
#   ./vault/init.sh
#
# - Initialises Vault with a single unseal key (fine for a one-machine lab)
# - Saves the unseal key to ~/.vault-unseal (mode 600), OUTSIDE this project
# - Prints the root token ONCE: put it in your password manager, never in this folder
# - Unseals Vault
set -euo pipefail

CONTAINER="${VAULT_CONTAINER:-enterprise-vault}"
KEYFILE="${VAULT_UNSEAL_FILE:-$HOME/.vault-unseal}"
HERE="$(cd "$(dirname "$0")" && pwd)"

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

# vault status exits 2 when sealed, but still prints JSON. Wait up to 60 s for it.
status=""
for _ in $(seq 1 60); do
  status="$(docker exec "$CONTAINER" vault status -format=json 2>/dev/null || true)"
  [ -n "$status" ] && break
  sleep 1
done
if [ -z "$status" ]; then
  echo "[!] Vault did not answer within 60 s. Check: docker logs $CONTAINER --tail 30" >&2
  exit 1
fi
if [ "$(json_field initialized <<<"$status")" = "True" ]; then
  echo "[*] Vault is already initialised. Nothing to do (use ./vault/unseal.sh to unseal)."
  exit 0
fi
if [ -e "$KEYFILE" ]; then
  echo "[!] $KEYFILE already exists but Vault is not initialised." >&2
  echo "    Move that file aside first (it belongs to an older Vault), then re-run." >&2
  exit 1
fi

umask 077
out="$(docker exec "$CONTAINER" vault operator init -key-shares=1 -key-threshold=1 -format=json)"

python3 -c 'import json,sys; print(json.load(sys.stdin)["unseal_keys_b64"][0])' <<<"$out" > "$KEYFILE"
chmod 600 "$KEYFILE"
root_token="$(json_field root_token <<<"$out")"
unset out

echo
echo "[*] Vault initialised. Unseal key saved to $KEYFILE (mode 600)."
echo
echo "    ROOT TOKEN (shown once - save it in your password manager now):"
echo "    $root_token"
echo
unset root_token

"$HERE/unseal.sh"
