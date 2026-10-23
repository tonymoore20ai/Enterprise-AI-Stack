# Setup from scratch

Builds the whole stack on a fresh machine. Allow about an hour; most of it is the ~20 GB model
download and the image builds. Run everything as your normal user from the project folder.

## 0. Prerequisites

| Need | Version / note |
|---|---|
| Ubuntu | 22.04 or 24.04 |
| NVIDIA GPU | 24 GB VRAM (RTX 3090/4090 class) |
| NVIDIA driver | **580 or newer** for the default vLLM image. With 525-579, change the `model-server` image to the `v0.32.0-cu129` tag. Check: `nvidia-smi` |
| Docker Engine + Compose v2 | your user in the `docker` group |
| NVIDIA Container Toolkit | so containers can use the GPU |
| Tools | `python3`, `python3-venv`, `openssl`, `gpg`, `libnss3-tools` |
| Disk | ~100 GB free (images, model weights, data) |

## 1. Get the code

```bash
git clone <your repo url> ~/enterprise-ai-stack && cd ~/enterprise-ai-stack
chmod +x vault/*.sh nginx/make_certs.sh backup/*.sh
```

## 2. Generate secrets (before any `docker compose` command)

Compose refuses to start until every variable in `.env.example` exists in `.env`.

```bash
python3 setup/init_env.py        # base secrets + seaweedfs/s3.json
python3 litellm/add_env.py       # LiteLLM + vLLM keys
python3 datastores/add_env.py    # Redis + Qdrant credentials
python3 ide/setup.py             # your user ID + IDE password (asks you for it)
```

## 3. Download the model

```bash
python3 -m venv ~/hf-cli && ~/hf-cli/bin/pip install -U "huggingface_hub[cli]"
~/hf-cli/bin/hf download Qwen/Qwen2.5-Coder-32B-Instruct-AWQ     # ~20 GB into ~/.cache/huggingface
```

vLLM runs with `HF_HUB_OFFLINE=1`, so the weights must be in the cache before it starts.
If `~/.cache/huggingface` is owned by root, run `sudo chown -R $USER:$USER ~/.cache/huggingface` first.

## 4. TLS certificates and host names

```bash
./nginx/make_certs.sh            # local CA in ~/.local-ca + gateway certificate

# trust the CA: system/curl, then Chrome/Chromium (Firefox: import ~/.local-ca/ca.crt under Authorities)
sudo cp ~/.local-ca/ca.crt /usr/local/share/ca-certificates/ai-internal-ca.crt && sudo update-ca-certificates
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "Enterprise AI Stack Local CA" -i ~/.local-ca/ca.crt

echo "127.0.0.1 langfuse.ai.internal grafana.ai.internal api.ai.internal llm.ai.internal code.ai.internal" \
  | sudo tee -a /etc/hosts
```

## 5. Build and start

```bash
docker compose build agent-engine vscode-ide      # 15-25 min the first time
docker compose up -d
docker logs -f vllm-inference                     # wait for "Application startup complete" (3-5 min)
```

`agent-api` will restart repeatedly until Vault holds its keys (step 7). That's expected.

## 6. Initialise Vault

```bash
./vault/init.sh
```

It saves the unseal key to `~/.vault-unseal` and prints the **root token once**. Keep it for
steps 7-9, then revoke it in step 10.

## 7. Langfuse keys into Vault

1. Open `https://langfuse.ai.internal` and sign in as `tony@enterprise.local`. The password is
   `LANGFUSE_INIT_USER_PASSWORD` in `.env`.
   To use your own email, change `LANGFUSE_INIT_USER_EMAIL` in `docker-compose.yml` before step 5.
2. Go to **Settings → API Keys → Create new API keys**, and keep the page open.
3. Load them into Vault and create the agent's AppRole:

```bash
./vault/bootstrap.sh             # paste: root token, then the pk-lf / sk-lf keys
```

## 8. LiteLLM virtual keys

```bash
python3 litellm/create_keys.py   # paste the root token; expect HTTP 200 then 401/403
```

## 9. Your chat API key

```bash
python3 vault/create_api_key.py <your-name>   # paste the root token; save the key it prints
```

## 10. Revoke the root token

```bash
read -rsp "Root token: " VAULT_TOKEN; echo; export VAULT_TOKEN
docker exec -e VAULT_TOKEN enterprise-vault vault token revoke -self; unset VAULT_TOKEN
```

To get a temporary root token later for admin work: `./vault/new_root.sh`.

## 11. Knowledge base, eval and first run

```bash
docker compose restart agent-engine agent-api
docker exec agent-orchestrator python ingest_knowledge.py             # 33 sections into Qdrant
docker exec agent-orchestrator python create_eval_dataset.py
docker exec agent-orchestrator python create_eval_dataset.py --hard
docker exec agent-orchestrator python run_eval.py --dataset guardrails-eval-hard --name first-run
```

Expect 41/41 correct, or close: run it three times before reading anything into one miss.

## 12. Survive reboots and keep backups

```bash
# auto-unseal at boot (edit User= and the paths in the unit if your user isn't tonymoore20)
sudo cp vault/eas-unseal.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable docker eas-unseal.service

# first backup - creates ~/.backup-passphrase: copy it into your password manager
./backup/backup.sh && ./backup/verify.sh
( crontab -l 2>/dev/null; echo "17 2 * * * $PWD/backup/backup.sh >> \$HOME/backups/backup.log 2>&1" ) | crontab -
```

## Check

```bash
curl -s https://api.ai.internal/health                       # {"status":"ok"}
docker exec enterprise-vault vault status | grep Sealed      # Sealed  false
docker compose ps --format 'table {{.Name}}\t{{.Status}}'    # everything Up
```

Then open `https://code.ai.internal`, `https://langfuse.ai.internal` and
`https://grafana.ai.internal`. The Grafana login is `admin` with `GRAFANA_ADMIN_PASSWORD` from `.env`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| A container keeps restarting with `permission denied` on a config file | The file isn't world-readable: `chmod 644 <file>` (and `755` on its folder) |
| Config change in `nginx.conf` not picked up | Single-file mounts don't see edits: `docker compose up -d --force-recreate gateway` |
| vLLM fails with CUDA errors at start | Driver too old for the image (see step 0), or another process holds the GPU |
| Vault: `Failed to lock memory` | The `memlock` ulimit on the `vault` service is missing; it's in `docker-compose.yml` |
| Vault: `permission denied` on generate-root | `vault/config/vault.hcl` needs `enable_unauthenticated_access = ["generate-root"]`, then restart Vault |
| Agent: `Vault is sealed` | `./vault/unseal.sh` |
