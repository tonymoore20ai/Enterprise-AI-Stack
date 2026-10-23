#!/usr/bin/env bash
# One-time setup: adds the Ollama backend and the backend switch script.
# Safe to re-run (skips anything already done). Restores the backup if the
# compose file stops validating.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f docker-compose.yml ] || { echo "Run from the stack repo" >&2; exit 1; }
cp docker-compose.yml docker-compose.yml.pre-ollama
restore() { echo "!! Something failed - restoring docker-compose.yml" >&2; cp docker-compose.yml.pre-ollama docker-compose.yml; }
trap restore ERR

# 1. Ollama service, inserted just before the top-level `volumes:` section
if grep -q 'container_name: ollama' docker-compose.yml; then
  echo "= ollama service already present"
else
  awk '
    /^volumes:/ && !done {
      print "  # Alternative model backend. Only one backend fits on the GPU at a time,"
      print "  # so this is off by default - scripts/switch_backend.sh starts and stops it."
      print "  ollama:"
      print "    image: ollama/ollama:latest      # pin a version once you have tested it"
      print "    container_name: ollama"
      print "    runtime: nvidia"
      print "    profiles: [\"ollama\"]             # not started by a plain docker compose up -d"
      print "    environment:"
      print "      - NVIDIA_VISIBLE_DEVICES=all"
      print "      - OLLAMA_KEEP_ALIVE=24h        # keep the model loaded between requests"
      print "      - OLLAMA_CONTEXT_LENGTH=8192"
      print "    volumes:"
      print "      - ollama_data:/root/.ollama"
      print "    networks:"
      print "      - enterprise-ai-mesh"
      print "    # no ports: internal only. Ollama has no authentication."
      print ""
      done = 1
    }
    { print }
  ' docker-compose.yml > docker-compose.yml.tmp
  grep -q 'container_name: ollama' docker-compose.yml.tmp || { echo "No top-level volumes: section found" >&2; false; }
  mv docker-compose.yml.tmp docker-compose.yml
  echo "+ added ollama service"
fi

# 2. ollama_data volume, right after the top-level `volumes:` line
if grep -qE '^\s+ollama_data:\s*$' docker-compose.yml; then
  echo "= ollama_data volume already present"
else
  sed -i '/^volumes:/a\  ollama_data:' docker-compose.yml
  echo "+ added ollama_data volume"
fi

# 3. Validate the compose file with Docker itself
docker compose config -q
echo "+ docker-compose.yml validates"
trap - ERR

# 4. Switch script
mkdir -p scripts
echo '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' | base64 -d | gunzip > scripts/switch_backend.sh
chmod +x scripts/switch_backend.sh
echo "+ installed scripts/switch_backend.sh"

# 5. Docs: Ollama row in the internal-services table
if [ -f docs/SERVICE-ACCESS.md ] && ! grep -q '^| Ollama |' docs/SERVICE-ACCESS.md; then
  sed -i '/^| vLLM |/a | Ollama | `http://ollama:11434` | LiteLLM, only while the Ollama backend is active. No auth: never publish its port |' docs/SERVICE-ACCESS.md
  echo "+ documented Ollama in docs/SERVICE-ACCESS.md"
fi

# 6. Keep backup files out of git
for f in litellm/config.yaml.bak docker-compose.yml.pre-ollama; do
  grep -qxF "$f" .gitignore 2>/dev/null || echo "$f" >> .gitignore
done

echo
git status --short
echo
echo "Done. Review with: git diff"
echo "Check:  scripts/switch_backend.sh status"
