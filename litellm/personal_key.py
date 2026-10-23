#!/usr/bin/env python3
"""
Create a personal LiteLLM virtual key for calling the model directly
(https://llm.ai.internal, OpenAI-compatible). Run on the host from the project folder:

  python3 litellm/personal_key.py tony          # 'weather-agent' model, 60 requests/min

Re-running replaces that person's key (the old one stops working). The key is printed
once and not stored anywhere - put it in your password manager. Calls with this key are
counted per key in LiteLLM (usage, limits) but are not traced in Langfuse.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")


def env_value(name):
    text = open(os.path.join(ROOT, ".env")).read()
    m = re.search(rf"^{name}='([^']*)'", text, re.M)
    if not m:
        sys.exit(f"[!] {name} not in .env")
    return m.group(1)


def call(path, body, master):
    req = urllib.request.Request(GATEWAY + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {master}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", sys.argv[1]):
        sys.exit("usage: python3 litellm/personal_key.py <name>")
    name, alias = sys.argv[1], f"personal-{sys.argv[1]}"
    master = env_value("LITELLM_MASTER_KEY")
    try:
        call("/key/delete", {"key_aliases": [alias]}, master)
    except urllib.error.HTTPError:
        pass
    key = call("/key/generate", {"key_alias": alias, "models": ["weather-agent"],
                                 "rpm_limit": 60, "max_parallel_requests": 2,
                                 "metadata": {"owner": name, "type": "personal"}}, master)["key"]
    print(f"[*] Personal LiteLLM key for '{name}' (shown once - save it in your password manager):")
    print(f"\n    {key}\n")
    print("    Try it:")
    print("    curl -s https://llm.ai.internal/v1/chat/completions -H \"Authorization: Bearer <key>\" \\")
    print("      -H 'Content-Type: application/json' \\")
    print("      -d '{\"model\":\"weather-agent\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}]}'")


if __name__ == "__main__":
    main()
