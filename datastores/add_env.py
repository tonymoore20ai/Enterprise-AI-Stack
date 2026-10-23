#!/usr/bin/env python3
"""
Add passwords for Redis and Qdrant to .env (random; existing values are kept).
Run once on the host from the project folder:

  python3 datastores/add_env.py

Adds (only if missing):
  REDIS_PASSWORD   required by Redis (agent memory + Langfuse queue)
  QDRANT_API_KEY   required by Qdrant (knowledge base)
Used by: Redis, Qdrant, Langfuse server/worker, agent-engine, agent-api, the IDE.
Prints only variable names, never values.
"""
import os
import re
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
NEW = ("REDIS_PASSWORD", "QDRANT_API_KEY")


def main():
    if not os.path.exists(ENV):
        sys.exit(f"[!] {ENV} not found")
    text = open(ENV).read()
    present = set(re.findall(r"^([A-Z0-9_]+)=", text, re.M))
    missing = [k for k in NEW if k not in present]
    if not missing:
        print("[*] REDIS_PASSWORD and QDRANT_API_KEY already in .env - nothing to do.")
        return
    lines = ["", "# Redis + Qdrant (added by datastores/add_env.py)"]
    lines += [f"{k}='{secrets.token_hex(24)}'" for k in missing]  # hex: safe in URLs and shells
    with open(ENV, "a") as f:
        if not text.endswith("\n"):
            f.write("\n")
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)
    print(f"[*] Added to .env (mode 600): {', '.join(missing)}")


if __name__ == "__main__":
    main()
