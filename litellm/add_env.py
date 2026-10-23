#!/usr/bin/env python3
"""
Add the LiteLLM gateway secrets to .env (generated randomly; existing values are kept).
Run once on the host from the project folder:

  python3 litellm/add_env.py

Adds (only if missing):
  LITELLM_MASTER_KEY   admin key for the gateway (never give this to a client)
  LITELLM_SALT_KEY     encrypts stored credentials - never change it once keys exist
  LITELLM_DB_PASSWORD  password for the litellm-db Postgres
  VLLM_API_KEY         key vLLM now requires; only LiteLLM knows it
Prints only variable names, never values.
"""
import os
import re
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")

NEW = {
    "LITELLM_MASTER_KEY": lambda: "sk-" + secrets.token_urlsafe(32),
    "LITELLM_SALT_KEY": lambda: "sk-" + secrets.token_urlsafe(32),
    "LITELLM_DB_PASSWORD": lambda: secrets.token_urlsafe(24),
    "VLLM_API_KEY": lambda: secrets.token_urlsafe(32),
}


def main():
    if not os.path.exists(ENV):
        sys.exit(f"[!] {ENV} not found - create it first with vault/make_env.py")
    existing = open(ENV).read()
    present = set(re.findall(r"^([A-Z0-9_]+)=", existing, re.M))
    missing = [k for k in NEW if k not in present]
    if not missing:
        print("[*] All LiteLLM variables already in .env - nothing to do.")
        return
    lines = ["", "# LiteLLM gateway (added by litellm/add_env.py)"]
    lines += [f"{k}='{NEW[k]()}'" for k in missing]
    with open(ENV, "a") as f:
        if not existing.endswith("\n"):
            f.write("\n")
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)
    print(f"[*] Added to .env (mode 600): {', '.join(missing)}")


if __name__ == "__main__":
    main()
