#!/usr/bin/env bash
# Run at boot by the eas-unseal systemd service (see vault/eas-unseal.service).
# Waits for Docker and the Vault container, unseals Vault with ~/.vault-unseal,
# then restarts the services that read their secrets from Vault at startup.
# Safe to run by hand too:  ./vault/boot-unseal.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WAIT_SECONDS="${WAIT_SECONDS:-600}"   # Docker + Vault can take a while after a cold boot
log() { echo "[boot-unseal] $*"; }

# 1. Docker daemon
for ((i = 0; i < WAIT_SECONDS; i += 5)); do
  docker info >/dev/null 2>&1 && break
  sleep 5
done
docker info >/dev/null 2>&1 || { log "Docker did not come up within ${WAIT_SECONDS}s"; exit 1; }
log "Docker is up"

# 2. Vault container answering (vault status: 0 = unsealed, 2 = sealed, else not ready)
for ((i = 0; i < WAIT_SECONDS; i += 5)); do
  set +e; docker exec enterprise-vault vault status >/dev/null 2>&1; rc=$?; set -e
  [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ] && break
  sleep 5
done

# 3. Unseal (unseal.sh is a no-op if Vault is already unsealed)
"$HERE/unseal.sh"

# 4. Services that load secrets from Vault when they start may have given up while
#    Vault was sealed; restart them so they come up with their keys.
cd "$ROOT"
docker compose restart agent-api >/dev/null
log "Restarted agent-api"
log "Done"
