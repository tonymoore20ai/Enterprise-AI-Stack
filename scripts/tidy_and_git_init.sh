#!/usr/bin/env bash
# Tidy the project and prepare its first git commit - with a secret check that must pass.
# Run on the host from anywhere:
#   ./scripts/tidy_and_git_init.sh
#
# 1. MOVES (never deletes) backups and leftovers to ~/eas-archive/<date>/:
#    every *.bak-* file, the unused root requirements.txt, the old init_langfuse.py
#    experiment, and the one-time vault/make_env.py migration script.
# 2. Sets the executable bit on shell scripts, so git records it.
# 3. git init (if needed) and stages everything .gitignore allows.
# 4. Refuses - and unstages everything - if any staged file contains a value from .env,
#    the AppRole credentials, the unseal key, or anything that looks like a real key.
#    It prints file names only, never the secrets.
# It does NOT commit: you review and commit yourself (commands printed at the end).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
command -v git >/dev/null || { echo "[!] git is not installed (sudo apt install git)" >&2; exit 1; }

# 1. Archive backups and leftovers
ARCHIVE="$HOME/eas-archive/$(date +%Y%m%d-%H%M%S)"
moved=0
archive() {  # archive <path relative to ROOT>
  [ -e "$1" ] || return 0
  mkdir -p "$ARCHIVE/$(dirname "$1")"
  mv "$1" "$ARCHIVE/$1"
  moved=$((moved + 1))
}
while IFS= read -r -d '' f; do archive "${f#./}"; done \
  < <(find . -path ./.git -prune -o -type f -name '*.bak-*' -print0)
archive requirements.txt                       # unused (images use developer-workspace/requirements.txt)
archive developer-workspace/init_langfuse.py   # early raw-OTLP experiment
archive vault/make_env.py                      # one-time migration; needs a backup that no longer exists
[ "$moved" -gt 0 ] && echo "[*] Moved $moved file(s) to $ARCHIVE" || echo "[*] Nothing to archive"

# 2. Executable bits
chmod +x vault/*.sh nginx/make_certs.sh backup/*.sh ide/init-extensions.sh scripts/*.sh 2>/dev/null || true

# 3. git init + stage
[ -d .git ] || { git init -q -b main; echo "[*] Initialised git repository (branch main)"; }
git add -A

fail() { echo "[!] $1" >&2; git reset -q; echo "[!] Unstaged everything - nothing will be committed. Fix the above, then re-run." >&2; exit 1; }

# 4a. Files that must never be committed
bad_paths="$(git diff --cached --name-only | grep -E '(^|/)\.env$|^secrets\.txt$|^vault-approle-secrets/|^seaweedfs/s3\.json$|^nginx/certs/|\.bak-' || true)"
[ -z "$bad_paths" ] || fail "These secret files are staged (check .gitignore):
$bad_paths"

# 4b. Any real secret value inside a staged file
PATTERNS="$(mktemp)"; chmod 600 "$PATTERNS"; trap 'rm -f "$PATTERNS"' EXIT
python3 - "$PATTERNS" <<'EOF'
import os, re, sys
vals = set()
if os.path.exists(".env"):
    for k, v in re.findall(r"^([A-Z0-9_]+)='?([^'\n]*)'?$", open(".env").read(), re.M):
        if len(v) >= 8 and not k.startswith("HOST_"):
            vals.add(v)
for p in ("vault-approle-secrets/role_id", "vault-approle-secrets/secret_id",
          os.path.expanduser("~/.vault-unseal"), os.path.expanduser("~/.backup-passphrase")):
    try:
        v = open(p).read().strip()
        if len(v) >= 8:
            vals.add(v)
    except OSError:
        pass
open(sys.argv[1], "w").write("\n".join(sorted(vals)) + "\n")
EOF
leak_files="$(git grep --cached -l -F -f "$PATTERNS" 2>/dev/null || true)"
[ -z "$leak_files" ] || fail "These staged files contain a real secret value (.env, AppRole or unseal key):
$leak_files"

# 4c. Anything that looks like a real key, even if it isn't in .env
key_files="$(git grep --cached -l -E \
  'sk-lf-[0-9a-f]{8}-[0-9a-f]{4}|pk-lf-[0-9a-f]{8}-[0-9a-f]{4}|hvs\.[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY|\$argon2id\$v=|wa_[A-Za-z0-9_-]{30,}' \
  2>/dev/null || true)"
[ -z "$key_files" ] || fail "These staged files contain something that looks like a real key:
$key_files"

count="$(git diff --cached --name-only | wc -l)"
echo "[*] Secret check passed: $count files staged, no secrets found."
echo
echo "Review what will be committed:   git status"
if [ -z "$(git config user.email || true)" ]; then
  echo "Set your identity (once):        git config --global user.name \"Your Name\""
  echo "                                 git config --global user.email you@example.com"
fi
echo "Commit:                          git commit -m \"Initial commit: enterprise AI stack\""
echo "Then (optional) push to GitHub:  git remote add origin <url> && git push -u origin main"
