#!/usr/bin/with-contenv bash
# Installs VS Code extensions (from Open VSX) the first time the IDE starts.
# Already-installed extensions are skipped, so later starts are quick.
# Add or remove extension IDs below and restart the container.
# Failures are reported in: docker logs enterprise-vscode

EXTENSIONS=(
  ms-python.vscode-python-envs  # Python Environments - required by the Python extension
  ms-python.python          # Python: run, test, environments
  ms-python.debugpy         # Python debugger
  detachhead.basedpyright   # type checking + IntelliSense (Pylance isn't on Open VSX)
  charliermarsh.ruff        # lint + format
  ms-toolsai.jupyter        # notebooks
  redhat.vscode-yaml        # docker-compose / config YAML
  ms-azuretools.vscode-docker  # Dockerfile + compose editing
  mhutchie.git-graph        # git history view
)

CS=/app/code-server/bin/code-server
DIR=/config/extensions
run_cs() { HOME=/config s6-setuidgid abc "$CS" --extensions-dir "$DIR" "$@"; }
installed="$(run_cs --list-extensions 2>/dev/null | tr '[:upper:]' '[:lower:]')"

failed=()
for ext in "${EXTENSIONS[@]}"; do
  grep -qx "${ext,,}" <<<"$installed" && continue
  echo "**** installing VS Code extension: $ext ****"
  ok=0
  for attempt in 1 2 3; do                       # retry: Open VSX can be briefly unreachable
    if out="$(run_cs --install-extension "$ext" 2>&1)"; then ok=1; break; fi
    sleep $((attempt * 5))
  done
  if [ "$ok" = 0 ]; then
    failed+=("$ext")
    echo "**** FAILED to install $ext: $(tail -n 3 <<<"$out") ****"
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "**** Extensions not installed: ${failed[*]} - restart the container to retry ****"
fi
