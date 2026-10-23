#!/usr/bin/env python3
"""
Create the gateway's virtual keys and store them in Vault (secret/agent/litellm).
Run on the host from the project folder, once LiteLLM is up:

  ./vault/new_root.sh            # temporary root token (revoke it afterwards)
  python3 litellm/create_keys.py

Keys created (re-running replaces them; the old ones stop working):
  agent-engine  -> model 'weather-agent' only, 120 requests/min, 8 in parallel
  guardrails    -> model 'guardrails' only,    300 requests/min, 8 in parallel
Nothing secret is printed or written to disk.
"""
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
VAULT_CONTAINER = os.environ.get("VAULT_CONTAINER", "enterprise-vault")

KEYS = {
    "agent_key": {"key_alias": "agent-engine", "models": ["weather-agent"],
                  "rpm_limit": 120, "max_parallel_requests": 8,
                  "metadata": {"owner": "agent-engine"}},
    "rails_key": {"key_alias": "guardrails", "models": ["guardrails"],
                  "rpm_limit": 300, "max_parallel_requests": 8,
                  "metadata": {"owner": "nemo-guardrails"}},
}


def env_value(name):
    text = open(os.path.join(ROOT, ".env")).read()
    m = re.search(rf"^{name}='([^']*)'", text, re.M) or re.search(rf"^{name}=(\S+)", text, re.M)
    if not m:
        sys.exit(f"[!] {name} not in .env - run python3 litellm/add_env.py first")
    return m.group(1)


def call(path, body, master):
    req = urllib.request.Request(
        GATEWAY + path, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {master}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def wait_for_gateway():
    for _ in range(60):
        try:
            with urllib.request.urlopen(GATEWAY + "/health/liveliness", timeout=3):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    sys.exit(f"[!] LiteLLM is not answering at {GATEWAY} - check: docker logs litellm --tail 30")


def main():
    master = env_value("LITELLM_MASTER_KEY")
    root_token = getpass.getpass("Vault root token: ")
    wait_for_gateway()

    # Remove keys from an earlier run (ignore "not found")
    try:
        call("/key/delete", {"key_aliases": [k["key_alias"] for k in KEYS.values()]}, master)
        print("[*] Removed previous agent-engine / guardrails keys")
    except urllib.error.HTTPError:
        pass

    secret = {}
    for field, spec in KEYS.items():
        try:
            secret[field] = call("/key/generate", spec, master)["key"]
        except urllib.error.HTTPError as e:
            sys.exit(f"[!] LiteLLM refused to create {spec['key_alias']}: HTTP {e.code} {e.read()[:200]!r}")
        print(f"[*] Created key {spec['key_alias']} (models: {', '.join(spec['models'])}, "
              f"{spec['rpm_limit']} rpm)")

    # Store in Vault: JSON on stdin, token by env name -> nothing on the command line
    proc = subprocess.run(
        ["docker", "exec", "-i", "-e", "VAULT_TOKEN", VAULT_CONTAINER,
         "vault", "kv", "put", "secret/agent/litellm", "-"],
        input=json.dumps(secret).encode(), capture_output=True,
        env={**os.environ, "VAULT_TOKEN": root_token})
    if proc.returncode != 0:
        sys.exit(f"[!] Vault write failed: {proc.stderr.decode()[:300]}")
    print("[*] Stored both keys in Vault at secret/agent/litellm")

    # Check the agent key works and is limited to its own model
    def chat(key, model):
        req = urllib.request.Request(
            GATEWAY + "/v1/chat/completions", method="POST",
            data=json.dumps({"model": model, "max_tokens": 3,
                             "messages": [{"role": "user", "content": "Say OK"}]}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    ok = chat(secret["agent_key"], "weather-agent")
    denied = chat(secret["agent_key"], "guardrails")
    print(f"[*] agent key -> weather-agent: HTTP {ok} (expect 200)")
    print(f"[*] agent key -> guardrails:    HTTP {denied} (expect 401/403: not allowed)")
    if ok != 200:
        sys.exit("[!] The agent key could not reach the model - check: docker logs litellm --tail 30")


if __name__ == "__main__":
    main()
