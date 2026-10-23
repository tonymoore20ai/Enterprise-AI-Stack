#!/usr/bin/env bash
# Switch the stack's model backend between vLLM (Hugging Face models) and Ollama.
#
#   scripts/switch_backend.sh vllm      # vLLM serves the model set by --model in docker-compose.yml
#   scripts/switch_backend.sh ollama    # Ollama serves $OLLAMA_MODEL (default below)
#   scripts/switch_backend.sh status    # show which backend LiteLLM points at
#
#   OLLAMA_MODEL=llama3.1:8b scripts/switch_backend.sh ollama   # try a different Ollama model
#
# The agent and guardrails never change: they always call LiteLLM aliases
# (weather-agent, guardrails). This script only rewrites where those aliases
# point in litellm/config.yaml, and stops one GPU backend before starting the
# other - a single 24 GB card can't hold both 32B models at once.
#
# After switching, run the eval to measure the change:
#   docker exec agent-orchestrator python run_eval.py --name <backend>-<model> --description "..."

set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG=litellm/config.yaml
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:32b}"
# vLLM's model comes from its --model flag, so the two can never disagree
VLLM_MODEL="$(grep -oP -- '--model\s+\K\S+' docker-compose.yml | head -1)"

current() {
  if grep -qE '^\s*model:\s*ollama_chat/' "$CONFIG"; then echo ollama; else echo vllm; fi
}

point_litellm_at() {   # $1 = model string, $2 = api_base
  cp "$CONFIG" "$CONFIG.bak"
  # Rewrites every model entry's model and api_base lines (both aliases use the same backend)
  sed -i -E \
    -e "s#^(\s*model:\s*)(hosted_vllm|ollama_chat)/.*#\1$1#" \
    -e "s#^(\s*api_base:\s*).*#\1$2#" \
    "$CONFIG"
  echo "LiteLLM now points at: $1 ($2)"
}

wait_for() {   # $1 = description, $2... = command that succeeds when ready
  local what="$1"; shift
  printf 'Waiting for %s' "$what"
  for _ in $(seq 1 120); do          # up to 10 minutes; a 32B model takes a while to load
    if "$@" >/dev/null 2>&1; then echo " ready."; return 0; fi
    printf '.'; sleep 5
  done
  echo " timed out." >&2; return 1
}

case "${1:-}" in
  vllm)
    [ -n "$VLLM_MODEL" ] || { echo "Could not find --model in docker-compose.yml" >&2; exit 1; }
    docker compose stop ollama 2>/dev/null || true
    point_litellm_at "hosted_vllm/$VLLM_MODEL" "http://model-server:8000/v1"
    docker compose up -d model-server
    since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    wait_for "vLLM to load $VLLM_MODEL" \
      sh -c "docker logs --since '$since' vllm-inference 2>&1 | grep -q 'Application startup complete'"
    ;;
  ollama)
    docker compose stop model-server
    point_litellm_at "ollama_chat/$OLLAMA_MODEL" "http://ollama:11434"
    docker compose --profile ollama up -d ollama
    wait_for "Ollama" docker exec ollama ollama list
    if ! docker exec ollama ollama list | awk 'NR>1 {print $1}' | grep -qx "$OLLAMA_MODEL"; then
      echo "Pulling $OLLAMA_MODEL (first time only; ~20 GB for a 32B model)..."
      docker exec ollama ollama pull "$OLLAMA_MODEL"
    fi
    ;;
  status)
    echo "Active backend: $(current)"
    grep -nE '^\s*(model_name|model|api_base):' "$CONFIG"
    exit 0
    ;;
  *)
    sed -n '2,10p' "$0"; exit 1
    ;;
esac

docker compose restart litellm
echo
echo "Done. Active backend: $(current). Smoke test:"
echo "  docker exec agent-orchestrator python agent_loop.py"
