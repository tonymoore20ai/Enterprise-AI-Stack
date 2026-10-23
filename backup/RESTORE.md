# Restoring the enterprise AI stack from a backup

You need: the backup file (`eas-backup-*.tar.gpg`) and the backup passphrase
(from your password manager). Run everything as your normal user on the target machine,
with Docker and the NVIDIA container toolkit installed.

## 1. Decrypt and unpack

```bash
mkdir -p ~/restore && cd ~/restore
read -rsp "Backup passphrase: " P; echo
echo "$P" > ~/.backup-passphrase && chmod 600 ~/.backup-passphrase; unset P
gpg --batch --pinentry-mode loopback --passphrase-file ~/.backup-passphrase \
    -d /path/to/eas-backup-YYYYmmdd-HHMMSS.tar.gpg | tar xf -
cat MANIFEST.txt
```

## 2. Keys and project folder

```bash
tar xzf keys/vault-unseal.tar.gz -C ~        # ~/.vault-unseal
tar xzf keys/local-ca.tar.gz -C ~            # ~/.local-ca
tar xzf project/enterprise-ai-stack.tar.gz -C ~    # ~/enterprise-ai-stack incl. .env
cd ~/enterprise-ai-stack
```

Re-trust the CA in your OS and browser (same commands as the gateway setup), and add the
`*.ai.internal` names to `/etc/hosts`.

## 3. Volumes (before starting the services that use them)

Create each volume and unpack into it. Volume names are `enterprise-ai-stack_<name>`:

```bash
restore_vol() {
  docker volume create "enterprise-ai-stack_$1" >/dev/null
  docker run --rm -v "enterprise-ai-stack_$1":/v -v ~/restore/volumes:/in:ro alpine:3.20 \
    sh -c "cd /v && tar xzf /in/$1.tar.gz"
}
for v in vault_data redis_data grafana_data langfuse_ch_data seaweed_data; do
  [ -f ~/restore/volumes/$v.tar.gz ] && restore_vol $v
done
```

## 4. Postgres databases

Start only the databases, then load the dumps into them:

```bash
docker compose up -d langfuse-db litellm-db
sleep 10
docker exec -i langfuse-db pg_restore -U langfuse -d langfuse --clean --if-exists < ~/restore/db/langfuse.dump
docker exec -i litellm-db  pg_restore -U litellm  -d litellm  --clean --if-exists < ~/restore/db/litellm.dump
```

## 5. Start everything

```bash
docker compose build agent-engine vscode-ide
docker compose up -d
./vault/unseal.sh
docker compose restart agent-engine agent-api litellm
```

## 6. Rebuild what wasn't backed up

```bash
# model weights (HF cache) - see the model download steps
docker exec agent-orchestrator python ingest_knowledge.py     # Qdrant knowledge base
```

## 7. Check

```bash
docker exec agent-orchestrator python run_eval.py --dataset guardrails-eval-hard --name restore-check
```

Expect the same pass rate as before the restore (41/41 at the time of writing).
