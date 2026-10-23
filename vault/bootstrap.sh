#!/usr/bin/env bash
# Set up Vault for the agent. Run on the host from the project folder, after
# ./vault/init.sh:
#   ./vault/bootstrap.sh
#
# It asks for (input hidden, nothing is written to disk except the AppRole files):
#   - the Vault root token
#   - the Langfuse public and secret keys (create a NEW pair in the Langfuse UI first)
#
# What it sets up (safe to re-run; each run issues a fresh AppRole secret ID):
#   - KV v2 secrets engine at secret/
#   - secret/agent/langfuse            {public_key, secret_key}
#   - policy agent-read                read-only on secret/data/agent/*
#   - AppRole agent-engine             tokens get agent-read, 1 h TTL
#   - vault-approle-secrets/role_id and secret_id (mode 600), which the agent
#     container sees at /var/run/secrets/vault
set -euo pipefail

CONTAINER="${VAULT_CONTAINER:-enterprise-vault}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPROLE_DIR="$ROOT/vault-approle-secrets"

read -rsp "Vault root token: " VAULT_TOKEN; echo
read -rsp "Langfuse public key (pk-lf-...): " LF_PUBLIC_KEY; echo
read -rsp "Langfuse secret key (sk-lf-...): " LF_SECRET_KEY; echo
export VAULT_TOKEN LF_PUBLIC_KEY LF_SECRET_KEY

case "$LF_PUBLIC_KEY" in pk-lf-*) ;; *) echo "[!] Public key should start with pk-lf-" >&2; exit 1;; esac
case "$LF_SECRET_KEY" in sk-lf-*) ;; *) echo "[!] Secret key should start with sk-lf-" >&2; exit 1;; esac

# Pass the token by name (-e VAULT_TOKEN), so its value is not on the command line
v() { docker exec -i -e VAULT_TOKEN "$CONTAINER" vault "$@"; }

if ! v token lookup > /dev/null 2>&1; then
  echo "[!] Vault rejected the token, or Vault is sealed (run ./vault/unseal.sh)." >&2
  exit 1
fi

# 1. KV v2 at secret/
if v secrets list -format=json | python3 -c 'import json,sys; sys.exit(0 if "secret/" in json.load(sys.stdin) else 1)'; then
  echo "[*] KV engine secret/ already enabled"
else
  v secrets enable -path=secret -version=2 kv > /dev/null
  echo "[*] Enabled KV v2 at secret/"
fi

# 2. Langfuse keys (JSON on stdin, so the values are not on the command line)
python3 -c 'import json,os; print(json.dumps({"public_key": os.environ["LF_PUBLIC_KEY"], "secret_key": os.environ["LF_SECRET_KEY"]}))' \
  | v kv put secret/agent/langfuse - > /dev/null
unset LF_PUBLIC_KEY LF_SECRET_KEY
echo "[*] Wrote secret/agent/langfuse"

# 3. Read-only policy for the agent
v policy write agent-read - > /dev/null <<'EOF'
# Agent may read its own secrets and nothing else
path "secret/data/agent/*" {
  capabilities = ["read"]
}
EOF
echo "[*] Wrote policy agent-read"

# 4. AppRole for the agent container
if v auth list -format=json | python3 -c 'import json,sys; sys.exit(0 if "approle/" in json.load(sys.stdin) else 1)'; then
  echo "[*] AppRole auth already enabled"
else
  v auth enable approle > /dev/null
  echo "[*] Enabled AppRole auth"
fi
v write auth/approle/role/agent-engine \
  token_policies=agent-read token_ttl=1h token_max_ttl=4h \
  secret_id_ttl=0 secret_id_num_uses=0 > /dev/null
echo "[*] Wrote AppRole agent-engine"

# 5. Credentials for the agent, readable only by you (the container runs as root)
mkdir -p "$APPROLE_DIR"
chmod 700 "$APPROLE_DIR"
umask 077
v read -field=role_id auth/approle/role/agent-engine/role-id > "$APPROLE_DIR/role_id"
v write -f -field=secret_id auth/approle/role/agent-engine/secret-id > "$APPROLE_DIR/secret_id"
chmod 600 "$APPROLE_DIR/role_id" "$APPROLE_DIR/secret_id"
echo "[*] Wrote $APPROLE_DIR/role_id and secret_id (mode 600)"

# 6. Prove the AppRole can log in and read the Langfuse secret (prints no values)
login_json="$(python3 -c 'import json,sys; print(json.dumps({"role_id": open(sys.argv[1]).read().strip(), "secret_id": open(sys.argv[2]).read().strip()}))' \
  "$APPROLE_DIR/role_id" "$APPROLE_DIR/secret_id")"
agent_token="$(docker exec -i "$CONTAINER" vault write -format=json auth/approle/login - <<<"$login_json" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["auth"]["client_token"])')"
unset login_json
if VAULT_TOKEN="$agent_token" docker exec -i -e VAULT_TOKEN "$CONTAINER" \
     vault kv get -format=json secret/agent/langfuse > /dev/null 2>&1; then
  echo "[*] Check passed: AppRole agent-engine can read secret/agent/langfuse"
else
  echo "[!] Check failed: AppRole could not read secret/agent/langfuse" >&2
  exit 1
fi
unset agent_token VAULT_TOKEN
echo "[*] Done."
