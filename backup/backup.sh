#!/usr/bin/env bash
# Encrypted backup of the enterprise AI stack. Run on the host from anywhere:
#   ./backup/backup.sh                    # standard backup
#   ./backup/backup.sh --skip-traces      # skip Langfuse traces (no pause, much faster)
#   ./backup/backup.sh --with-otel        # also the OTel ClickHouse (Grafana traces; can be large)
#
# Writes one encrypted file per run to $BACKUP_DIR (default ~/backups/enterprise-ai-stack):
#   eas-backup-YYYYmmdd-HHMMSS.tar.gpg  (+ .sha256), keeping the newest $KEEP (default 14).
#
# What's inside (see MANIFEST.txt in each archive):
#   keys/       ~/.vault-unseal and ~/.local-ca (the CA)       - cannot be recreated
#   project/    this folder incl. .env and AppRole credentials  - all config + secrets
#   db/         pg_dump of Langfuse and LiteLLM Postgres        - users, projects, scores, keys
#   volumes/    Vault, Redis (agent memory), Grafana, Langfuse ClickHouse + SeaweedFS (traces)
# Not included (re-creatable): Qdrant (re-run ingest_knowledge.py), model weights,
#   embedding/IDE caches, OTel ClickHouse unless --with-otel.
#
# Encryption: AES-256 with the passphrase in ~/.backup-passphrase (created on first run).
# KEEP A COPY OF THAT PASSPHRASE IN YOUR PASSWORD MANAGER - without it no backup can be read.
#
# Nightly at 02:17 (crontab -e):
#   17 2 * * * /home/tonymoore20/enterprise-ai-stack/backup/backup.sh >> $HOME/backups/backup.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$ROOT")}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/enterprise-ai-stack}"
KEEP="${KEEP:-14}"
PASSFILE="${BACKUP_PASSPHRASE_FILE:-$HOME/.backup-passphrase}"
HELPER=alpine:3.20

SKIP_TRACES=0; WITH_OTEL=0
for arg in "$@"; do
  case "$arg" in
    --skip-traces) SKIP_TRACES=1 ;;
    --with-otel)   WITH_OTEL=1 ;;
    -h|--help)     sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "[!] unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

log()  { echo "[$(date +%H:%M:%S)] $*"; }
warn() { echo "[$(date +%H:%M:%S)] [!] $*" >&2; WARNINGS+=("$*"); }
for c in docker gpg tar sha256sum; do
  command -v "$c" >/dev/null || { echo "[!] $c is not installed" >&2; exit 1; }
done

umask 077
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
WARNINGS=(); STOPPED=()

if [ ! -s "$PASSFILE" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '\n' > "$PASSFILE"; chmod 600 "$PASSFILE"
  cat <<EOF

  ************************************************************************
  Created the backup passphrase in $PASSFILE
  Copy it into your password manager NOW:   cat $PASSFILE
  If this disk dies, that copy is the only way to open your backups.
  ************************************************************************

EOF
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d "$BACKUP_DIR/.stage-$STAMP-XXXX")"
mkdir -p "$STAGE"/{keys,project,db,volumes}

cleanup() {
  local rc=$?
  if [ "${#STOPPED[@]}" -gt 0 ]; then
    log "Restarting ${STOPPED[*]}"
    docker compose start "${STOPPED[@]}" >/dev/null 2>&1 \
      || echo "[!] Could not restart ${STOPPED[*]} - run: cd $ROOT && docker compose up -d" >&2
  fi
  rm -rf "$STAGE" 2>/dev/null || true
  rm -f "${PARTIAL:-}" 2>/dev/null || true   # never leave a half-written archive behind
  [ "$rc" -ne 0 ] && echo "[!] Backup FAILED (exit $rc) - nothing was rotated away." >&2
  exit "$rc"
}
trap cleanup EXIT

running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

vol_tar() {  # vol_tar <compose volume name>
  local vol="${PROJECT}_$1"
  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    warn "volume $vol not found - skipped"; return
  fi
  docker run --rm -v "$vol":/v:ro -v "$STAGE/volumes":/out "$HELPER" \
    tar czf "/out/$1.tar.gz" -C /v .
  log "  volume $1 ($(du -h "$STAGE/volumes/$1.tar.gz" | cut -f1))"
}

pg_dump_to() {  # pg_dump_to <container> <user> <db>
  if ! running "$1"; then warn "$1 is not running - $3 database skipped"; return; fi
  docker exec "$1" pg_dump -U "$2" -d "$3" -Fc > "$STAGE/db/$3.dump"
  log "  database $3 ($(du -h "$STAGE/db/$3.dump" | cut -f1))"
}

log "Backup $STAMP -> $BACKUP_DIR"

# 1. Keys that cannot be recreated
log "Keys"
for item in .vault-unseal .local-ca; do
  if [ -e "$HOME/$item" ]; then
    tar czf "$STAGE/keys/${item#.}.tar.gz" -C "$HOME" "$item"
    log "  ~/$item"
  else
    warn "~/$item not found - skipped"
  fi
done

# 2. Project folder: config, .env, AppRole credentials, knowledge docs, certs
log "Project folder"
tar czf "$STAGE/project/$(basename "$ROOT").tar.gz" --exclude='__pycache__' \
  -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
log "  $(basename "$ROOT") ($(du -h "$STAGE/project/"*.tar.gz | cut -f1))"

# 3. Postgres: consistent logical dumps while running
log "Databases"
pg_dump_to langfuse-db langfuse langfuse
pg_dump_to litellm-db litellm litellm

# 4. Volumes that can be copied while running
log "Volumes"
vol_tar vault_data           # encrypted by Vault itself; writes only happen during admin work
if running agent-memory; then  # flush Redis to disk first (agent memory = db 2)
  before="$(docker exec agent-memory redis-cli LASTSAVE)"
  docker exec agent-memory redis-cli BGSAVE >/dev/null || true
  for _ in $(seq 1 30); do
    [ "$(docker exec agent-memory redis-cli LASTSAVE)" != "$before" ] && break
    sleep 1
  done
fi
vol_tar redis_data
vol_tar grafana_data

# 5. Langfuse traces and eval history: pause the worker and its ClickHouse briefly so
#    the copy is consistent. The Langfuse server keeps accepting traces meanwhile
#    (they queue in Redis/S3 and are processed when the worker restarts).
if [ "$SKIP_TRACES" = 0 ]; then
  log "Langfuse traces (pausing langfuse-worker + langfuse-clickhouse)"
  STOPPED=(langfuse-clickhouse langfuse-worker)
  docker compose stop langfuse-worker langfuse-clickhouse >/dev/null
  vol_tar langfuse_ch_data
  vol_tar seaweed_data
  docker compose start langfuse-clickhouse langfuse-worker >/dev/null
  STOPPED=()
  log "  Langfuse resumed"
else
  log "Langfuse traces: skipped (--skip-traces)"
fi

if [ "$WITH_OTEL" = 1 ]; then
  log "OTel ClickHouse (pausing otel-collector + clickhouse)"
  STOPPED=(clickhouse otel-collector)
  docker compose stop otel-collector clickhouse >/dev/null
  vol_tar clickhouse_data
  docker compose start clickhouse otel-collector >/dev/null
  STOPPED=()
fi

# 6. Manifest
{
  echo "Enterprise AI stack backup $STAMP"
  echo "Host: $(hostname)   Project: $ROOT   Compose project: $PROJECT"
  echo
  echo "Contents:"; (cd "$STAGE" && find . -type f ! -name MANIFEST.txt -exec du -h {} + | sort -k2)
  echo
  echo "Images in use:"; docker compose images 2>/dev/null || true
  echo
  if [ "${#WARNINGS[@]}" -gt 0 ]; then printf 'Warnings:\n'; printf '  - %s\n' "${WARNINGS[@]}"; fi
  echo "Restore: see project/.../backup/RESTORE.md"
} > "$STAGE/MANIFEST.txt"

# 7. Encrypt into one file, checksum, rotate
OUT="$BACKUP_DIR/eas-backup-$STAMP.tar.gpg"
PARTIAL="$OUT.partial"   # renamed only after encryption succeeded
log "Encrypting"
tar cf - -C "$STAGE" . | gpg --batch --yes --quiet --pinentry-mode loopback \
  --passphrase-file "$PASSFILE" --symmetric --cipher-algo AES256 -o "$PARTIAL"
chmod 600 "$PARTIAL"
mv "$PARTIAL" "$OUT"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

mapfile -t old < <(ls -1t "$BACKUP_DIR"/eas-backup-*.tar.gpg 2>/dev/null | tail -n +$((KEEP + 1)))
for f in "${old[@]}"; do rm -f "$f" "$f.sha256"; log "Rotated out $(basename "$f")"; done

log "Done: $OUT ($(du -h "$OUT" | cut -f1))"
if [ "${#WARNINGS[@]}" -gt 0 ]; then
  log "Finished with ${#WARNINGS[@]} warning(s):"; printf '    - %s\n' "${WARNINGS[@]}"
fi
log "Copy it off this machine (USB drive, NAS, cloud) - a backup on the same disk won't survive a disk failure."
