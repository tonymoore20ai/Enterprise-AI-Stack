# Service access

How to reach each part of the enterprise AI stack after the gateway lockdown.

All browser and API traffic goes through the **nginx gateway** (`enterprise-gateway`) on
port 443. Each service has its own hostname under `ai.internal`. Services are no longer
reached on `localhost:<port>`.

> **No credentials in this file.** Passwords, tokens and API keys live in Vault or in the
> local `.env` file, never in the repository. Ask the stack owner for access.

## Gateway URLs

| Service | URL | Backend container | What it is |
|---|---|---|---|
| Langfuse | https://langfuse.ai.internal | `langfuse-server:3000` | LLM tracing, datasets and evaluation runs |
| Grafana | https://grafana.ai.internal | `grafana:3000` | Dashboards over the OTel data in ClickHouse |
| Agent API | https://api.ai.internal | `agent-api:8080` | HTTP entry point to the agent (guarded by the NeMo rails) |
| LLM gateway | https://llm.ai.internal | `litellm:4000` | LiteLLM: OpenAI-compatible model access with per-user keys |
| VS Code | https://code.ai.internal | `vscode-ide:8443` | Browser IDE (code-server) on `developer-workspace/` |

The mapping comes from `nginx/nginx.conf`. Each server block sets `$upstream` and then
calls `proxy_pass $upstream`. To re-check it after a change:

```bash
grep -nE 'server_name|set \$upstream' nginx/nginx.conf
```

### Notes on the two API hostnames

- **`llm.ai.internal` is LiteLLM, not vLLM.** Clients never talk to vLLM directly. LiteLLM
  sits in front of it and adds per-user API keys, rate limits, budgets and model routing.
  Point any OpenAI-compatible client at `https://llm.ai.internal/v1` with a LiteLLM key.
  vLLM itself stays internal at `model-server:8000` and requires `VLLM_API_KEY`, which only
  LiteLLM holds, so every model call (including the agent's and the guardrail checks')
  must go through LiteLLM (`http://litellm:4000/v1` inside Docker).
- **`api.ai.internal` is the agent, not a raw model.** Requests here go through the agent,
  so they pass the NeMo Guardrails input and output rails and are traced in Langfuse. Use
  this for anything user-facing; use `llm.ai.internal` only for direct model access.

### Upstreams are resolved per request

`nginx.conf` resolves backend names through Docker's DNS when each request arrives, rather
than once at startup (the comment at line 20 explains this). As a result, nginx starts even
when a backend container is down. A stopped backend shows up as a `502` on its hostname
only, instead of taking the whole gateway down.

### Catch-all blocks

`nginx.conf` has `server_name _` default blocks on ports 80 and 443. Any request that does
not match one of the hostnames above, including `http://localhost` or the machine's bare IP,
is caught there and does not reach a service. This is intentional: there is no "default"
service to stumble into.

## One-time setup on each client machine

### 1. Resolve the hostnames

`ai.internal` is not a public domain, so each machine that opens these URLs must map the
hostnames to the stack host. On the stack host itself, add this line to `/etc/hosts`:

```
127.0.0.1  langfuse.ai.internal grafana.ai.internal api.ai.internal code.ai.internal llm.ai.internal
```

On another machine on the network, use the stack host's LAN IP instead of `127.0.0.1`.
For more than a few machines, add these records to the internal DNS server instead.

### 2. Trust the certificate

If the gateway uses a self-signed or internal-CA certificate, browsers show a warning until
the certificate (or its CA) is trusted on the client. On Ubuntu:

```bash
sudo cp <ca-cert>.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

Firefox keeps its own trust store, so import the certificate there separately.

## Services with no external address

These are reachable only from containers on the `enterprise-ai-mesh` Docker network, by
container name. That is deliberate: none of them should be exposed outside the host.

| Service | Internal address | Used by |
|---|---|---|
| LiteLLM | `http://litellm:4000` | other containers needing model access with LiteLLM keys |
| Agent API | `http://agent-api:8080` | nginx (`api.ai.internal`) |
| vLLM | `http://model-server:8000/v1` | LiteLLM only: vLLM requires `VLLM_API_KEY`, which only LiteLLM holds |
| Vault | `http://enterprise-vault:8200` | agent (secrets) |
| Qdrant | `http://vector-db:6333` | agent (vector memory / RAG) |
| Redis | `agent-memory:6379` | agent state, Langfuse queues |
| OTel collector | `http://agent-otel:4318` (HTTP), `agent-otel:4317` (gRPC) | agent tracing |
| ClickHouse (OTel) | `http://clickhouse:8123` | OTel collector, Grafana |
| Langfuse server (internal) | `http://langfuse-server:3000` | agent SDK (`LANGFUSE_HOST`) |
| Langfuse ClickHouse | `http://langfuse-clickhouse:8123` | Langfuse only |
| Langfuse Postgres | `langfuse-db:5432` | Langfuse only |
| SeaweedFS S3 | `http://seaweedfs:9090` | Langfuse event and media storage |

Containers keep using these internal addresses even after the lockdown. For example, the
agent still talks to `http://langfuse-server:3000`, not the gateway URL.

To check that nothing bypasses the gateway, list the published host ports:

```bash
docker compose ps --format "table {{.Service}}\t{{.Ports}}"
```

Only `enterprise-gateway` should publish ports (80 and 443). Anything else listed there is
reachable directly and skips nginx.

## Settings that must match the new URLs

Moving behind the gateway changes some public URLs that services embed in their own
config. Check these after any change to hostnames:

| Setting | Where | Should be | What breaks if it is wrong |
|---|---|---|---|
| `NEXTAUTH_URL` | Langfuse env in `docker-compose.yml` | `https://langfuse.ai.internal` | Login redirects bounce or fail |
| `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` | Langfuse env in `docker-compose.yml` | A URL the browser and SDK can reach | Image and audio uploads fail; text traces still work |
| `GF_SERVER_ROOT_URL` (if set) | Grafana env | `https://grafana.ai.internal` | Broken links and redirects in Grafana |

**Media uploads need a reachable S3 URL.** Langfuse hands out presigned URLs pointing at
`LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT`. That address must work from outside Docker. If
SeaweedFS is no longer published on the host, either route it through the gateway (for
example `https://s3.ai.internal`) or leave media uploads disabled. Event storage is
unaffected because it uses the internal `http://seaweedfs:9090` address.

## Quick health check

From the stack host:

```bash
for h in langfuse grafana code llm api; do
  printf '%-10s ' "$h"; curl -sk -o /dev/null -w '%{http_code}\n' "https://$h.ai.internal/"
done
```

A `200`, `302` or `401` means nginx routed the request to a running service. `502` means
nginx is up but the backend container is down. A connection error means the hostname does
not resolve: check `/etc/hosts`.
