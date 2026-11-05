#!/usr/bin/env bash
# Switch which backend serves each GPU's LiteLLM aliases: vLLM or Ollama.
#
#   scripts/switch_backend.sh vllm            # stop Ollama; vLLM serves everything
#   scripts/switch_backend.sh ollama 3090     # Ollama replaces model-server (strategist)
#   scripts/switch_backend.sh ollama 5060ti   # Ollama replaces model-server-2 (coder)
#   scripts/switch_backend.sh status          # show where each alias points
#
#   OLLAMA_MODEL=llama3.1:8b scripts/switch_backend.sh ollama 3090   # try a different model
#
# Ollama runs on one GPU at a time and takes over only that GPU's aliases. The
# vLLM server on the other GPU keeps running, and is started if it was stopped.
#   3090:   weather-agent, guardrails
#   5060ti: coder
# Agents and guardrails never change: they always call LiteLLM aliases. This
# script only rewrites where those aliases point in litellm/config.yaml.
#
# After a reboot Docker restarts the vLLM servers but not Ollama, so rerun this
# script if Ollama was active.
#
# After switching, run the eval to measure the change:
#   docker exec agent-orchestrator python run_eval.py --name <backend>-<model> --description "..."

set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG=litellm/config.yaml
STRATEGIST_ALIASES="weather-agent guardrails"
CODER_ALIASES="coder"

env_value() {   # $1 = variable name in .env; prints only that value
  grep -m1 "^$1=" .env | cut -d= -f2- | tr -d "\"'[:space:]"
}

point_aliases() {   # $1 = aliases, $2 = model string, $3 = api_base
  local alias
  for alias in $1; do
    grep -qE "model_name:\s*$alias\s*$" "$CONFIG" || { echo "Alias $alias not found in $CONFIG" >&2; exit 1; }
    # Only this alias's entry changes, from its model_name line to its api_base line
    sed -i -E "/model_name:\s*$alias\s*$/,/^\s*api_base:/{
      s#^(\s*model:\s*)(hosted_vllm|ollama_chat)/.*#\1$2#
      s#^(\s*api_base:\s*).*#\1$3#
    }" "$CONFIG"
    echo "LiteLLM: $alias -> $2 ($3)"
  done
}

point_strategist_at_vllm() { point_aliases "$STRATEGIST_ALIASES" "hosted_vllm/strategist" "http://model-server:8000/v1"; }
point_coder_at_vllm()      { point_aliases "$CODER_ALIASES" "hosted_vllm/coder" "http://model-server-2:8000/v1"; }

wait_for() {   # $1 = description, $2... = command that succeeds when ready
  local what="$1"; shift
  printf 'Waiting for %s' "$what"
  for _ in $(seq 1 120); do          # up to 10 minutes; a large model takes a while to load
    if "$@" >/dev/null 2>&1; then echo " ready."; return 0; fi
    printf '.'; sleep 5
  done
  echo " timed out." >&2; return 1
}

start_vllm() {   # $1 = compose service, $2 = container name; skips one already running
  if [ "$(docker inspect -f '{{.State.Running}}' "$2" 2>/dev/null)" = "true" ]; then
    echo "$1 is already running."
    return 0
  fi
  local since
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  docker compose up -d "$1"
  wait_for "$1 to load" \
    sh -c "docker logs --since '$since' '$2' 2>&1 | grep -q 'Application startup complete'"
}

case "${1:-}" in
  vllm)
    docker compose --profile ollama stop ollama 2>/dev/null || true
    cp "$CONFIG" "$CONFIG.bak"
    point_strategist_at_vllm
    point_coder_at_vllm
    start_vllm model-server vllm-inference
    start_vllm model-server-2 vllm-inference-coder
    ;;
  ollama)
    case "${2:-3090}" in
      3090)
        UUID_VAR=GPU_3090_UUID
        TARGET_SERVICE=model-server;    TARGET_ALIASES="$STRATEGIST_ALIASES"
        OTHER_SERVICE=model-server-2;   OTHER_CONTAINER=vllm-inference-coder
        OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:32b}"
        ;;
      5060ti)
        UUID_VAR=GPU_5060TI_UUID
        TARGET_SERVICE=model-server-2;  TARGET_ALIASES="$CODER_ALIASES"
        OTHER_SERVICE=model-server;     OTHER_CONTAINER=vllm-inference
        OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:9b}"
        ;;
      *)
        echo "Unknown GPU: ${2}. Use 3090 or 5060ti." >&2; exit 1
        ;;
    esac
    OLLAMA_GPU_UUID="$(env_value "$UUID_VAR")"
    [ -n "$OLLAMA_GPU_UUID" ] || { echo "$UUID_VAR is missing from .env" >&2; exit 1; }
    export OLLAMA_GPU_UUID

    # Start clean: Ollama may be on the other GPU from an earlier switch
    docker compose --profile ollama stop ollama 2>/dev/null || true
    cp "$CONFIG" "$CONFIG.bak"
    point_strategist_at_vllm
    point_coder_at_vllm

    docker compose stop "$TARGET_SERVICE"
    start_vllm "$OTHER_SERVICE" "$OTHER_CONTAINER"
    point_aliases "$TARGET_ALIASES" "ollama_chat/$OLLAMA_MODEL" "http://ollama:11434"

    docker compose --profile ollama up -d --force-recreate ollama   # applies the GPU choice
    wait_for "Ollama" docker exec ollama ollama list
    if ! docker exec ollama ollama list | awk 'NR>1 {print $1}' | grep -qx "$OLLAMA_MODEL"; then
      echo "Pulling $OLLAMA_MODEL (first time only)..."
      docker exec ollama ollama pull "$OLLAMA_MODEL"
    fi
    ;;
  status)
    grep -nE '^\s*(- )?(model_name|model|api_base):' "$CONFIG"
    exit 0
    ;;
  *)
    sed -n '2,10p' "$0"; exit 1
    ;;
esac

# --no-deps: litellm depends on model-server, which must stay stopped under Ollama
docker compose up -d --force-recreate --no-deps litellm   # reloads the rewritten config
echo
echo "Done. Current routes:"
grep -E '^\s*(- )?(model_name|model):' "$CONFIG"
