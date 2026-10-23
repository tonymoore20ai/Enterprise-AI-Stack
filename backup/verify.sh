#!/usr/bin/env bash
# Check that a backup can actually be opened and restored from - without touching the
# running stack. Run on the host:
#   ./backup/verify.sh                       # newest backup
#   ./backup/verify.sh path/to/eas-backup-20261022-201500.tar.gpg
#
# Checks: checksum, decryption with your passphrase, every inner archive is intact,
# and both Postgres dumps are readable by pg_restore. Decrypted data goes to a private
# temporary folder that is deleted afterwards.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/enterprise-ai-stack}"
PASSFILE="${BACKUP_PASSPHRASE_FILE:-$HOME/.backup-passphrase}"
ARCHIVE="${1:-$(ls -1t "$BACKUP_DIR"/eas-backup-*.tar.gpg 2>/dev/null | head -1)}"
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || { echo "[!] No backup found in $BACKUP_DIR" >&2; exit 1; }
[ -s "$PASSFILE" ] || { echo "[!] Passphrase file $PASSFILE not found" >&2; exit 1; }

umask 077
TMP="$(mktemp -d "$BACKUP_DIR/.verify-XXXX")"
trap 'rm -rf "$TMP"' EXIT
fail=0
ok()  { echo "  ok    $*"; }
bad() { echo "  FAIL  $*"; fail=1; }

echo "Verifying $(basename "$ARCHIVE")"

if [ -f "$ARCHIVE.sha256" ]; then
  (cd "$(dirname "$ARCHIVE")" && sha256sum -c --quiet "$(basename "$ARCHIVE").sha256") \
    && ok "checksum" || bad "checksum does not match (file corrupted or changed)"
else
  echo "  --    no checksum file"
fi

if gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASSFILE" \
     -d "$ARCHIVE" 2>/dev/null | tar xf - -C "$TMP"; then
  ok "decrypted with your passphrase"
else
  bad "could not decrypt (wrong passphrase or damaged file)"; exit 1
fi

for f in $(cd "$TMP" && find . -name '*.tar.gz' | sort); do
  tar tzf "$TMP/$f" >/dev/null 2>&1 && ok "${f#./}" || bad "${f#./} is damaged"
done

for d in "$TMP"/db/*.dump; do
  [ -e "$d" ] || continue
  if docker run --rm -v "$TMP/db":/d:ro postgres:16-alpine \
       pg_restore --list "/d/$(basename "$d")" >/dev/null 2>&1; then
    ok "db/$(basename "$d") readable by pg_restore"
  else
    bad "db/$(basename "$d") is not a valid dump"
  fi
done

echo; sed -n '/^Contents:/,/^$/p' "$TMP/MANIFEST.txt"
grep -A20 '^Warnings:' "$TMP/MANIFEST.txt" || true
echo
[ "$fail" = 0 ] && echo "Backup is good." || { echo "Backup has problems - see FAIL lines."; exit 1; }
