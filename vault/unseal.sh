#!/usr/bin/env bash
# Unseal Vault using the key in ~/.vault-unseal. Run on the host after every
# Vault restart (or reboot):
#   ./vault/unseal.sh
#
# To unseal automatically at boot, add to your crontab (crontab -e):
#   @reboot sleep 90 && /home/tonymoore20/enterprise-ai-stack/vault/unseal.sh >> /tmp/vault-unseal.log 2>&1
set -euo pipefail

CONTAINER="${VAULT_CONTAINER:-enterprise-vault}"
KEYFILE="${VAULT_UNSEAL_FILE:-$HOME/.vault-unseal}"

if [ ! -r "$KEYFILE" ]; then
  echo "[!] No unseal key at $KEYFILE (run ./vault/init.sh once first)." >&2
  exit 1
fi
perm="$(stat -c %a "$KEYFILE")"
if [ "$perm" != "600" ] && [ "$perm" != "400" ]; then
  echo "[!] $KEYFILE is mode $perm; fixing to 600."
  chmod 600 "$KEYFILE"
fi

# Wait up to 60 s for Vault to answer (exit 0 = unsealed, 2 = sealed)
status=""
for _ in $(seq 1 60); do
  set +e
  status="$(docker exec "$CONTAINER" vault status -format=json 2>/dev/null)"
  rc=$?
  set -e
  if [ $rc -eq 0 ] || [ $rc -eq 2 ]; then break; fi
  sleep 1
done
if [ -z "$status" ]; then
  echo "[!] Vault did not answer. Is the container running? docker compose up -d vault" >&2
  exit 1
fi

sealed="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["sealed"])' <<<"$status")"
if [ "$sealed" = "False" ]; then
  echo "[*] Vault is already unsealed."
  exit 0
fi

# The key goes in on stdin and is read inside the container, so it never appears
# on the host's command line (vault operator unseal has no stdin option itself)
docker exec -i "$CONTAINER" sh -c 'read -r key && exec vault operator unseal "$key"' \
  < "$KEYFILE" > /dev/null

if docker exec "$CONTAINER" vault status > /dev/null 2>&1; then
  echo "[*] Vault unsealed."
else
  echo "[!] Vault is still sealed - check: docker logs $CONTAINER" >&2
  exit 1
fi
