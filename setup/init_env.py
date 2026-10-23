#!/usr/bin/env python3
"""
Generate the base secrets for a FRESH install, and the matching SeaweedFS S3 config.
Run once from the project folder, before any other setup step:

  python3 setup/init_env.py

- Adds each variable to .env only if it is missing (safe to re-run; never overwrites),
  so on an existing install it changes nothing.
- Every service gets its own random password.
- Writes seaweedfs/s3.json from the S3 keys if that file doesn't exist yet.
Prints only variable names, never values. See .env.example for what each one is.
"""
import json
import os
import re
import secrets
import string

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
S3_JSON = os.path.join(ROOT, "seaweedfs", "s3.json")


def pw(n=32):  # letters + digits only: safe in URLs, YAML and shells
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


BASE = {
    "LANGFUSE_DB_PASSWORD": lambda: pw(),
    "LANGFUSE_CLICKHOUSE_PASSWORD": lambda: pw(),
    "OTEL_CLICKHOUSE_PASSWORD": lambda: pw(),
    "LANGFUSE_NEXTAUTH_SECRET": lambda: secrets.token_hex(32),
    "LANGFUSE_SALT": lambda: secrets.token_hex(16),
    "LANGFUSE_ENCRYPTION_KEY": lambda: secrets.token_hex(32),  # must be 64 hex characters
    "LANGFUSE_INIT_USER_PASSWORD": lambda: pw(20),
    "GRAFANA_ADMIN_PASSWORD": lambda: pw(20),
    "S3_ACCESS_KEY": lambda: "lf" + pw(14).lower(),
    "S3_SECRET_KEY": lambda: pw(40),
}


def read_env():
    if not os.path.exists(ENV):
        return "", {}
    text = open(ENV).read()
    vals = dict(re.findall(r"^([A-Z0-9_]+)='?([^'\n]*)'?$", text, re.M))
    return text, vals


def main():
    text, vals = read_env()
    missing = [k for k in BASE if k not in vals]
    if missing:
        new = {k: BASE[k]() for k in missing}
        block = ["", "# Base secrets (added by setup/init_env.py)"] + [f"{k}='{v}'" for k, v in new.items()]
        fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("\n".join(block) + "\n")
        vals.update(new)
        print(f"[*] Added to .env (mode 600): {', '.join(missing)}")
    else:
        print("[*] Base secrets already in .env - nothing to add.")
    os.chmod(ENV, 0o600)

    if not os.path.exists(S3_JSON):
        cfg = {"identities": [{
            "name": "langfuse",
            "credentials": [{"accessKey": vals["S3_ACCESS_KEY"], "secretKey": vals["S3_SECRET_KEY"]}],
            "actions": ["Admin", "Read", "Write", "List", "Tagging"],
        }]}
        os.makedirs(os.path.dirname(S3_JSON), exist_ok=True)
        fd = os.open(S3_JSON, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)  # SeaweedFS must read it
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        print("[*] Wrote seaweedfs/s3.json from the S3 keys in .env")
    else:
        print("[*] seaweedfs/s3.json already exists - left unchanged.")


if __name__ == "__main__":
    main()
