#!/usr/bin/env python3
"""
Create (or revoke) an API key for the chat API at https://api.ai.internal.
Run on the host from the project folder, with a temporary root token:

  ./vault/new_root.sh
  python3 vault/create_api_key.py tony            # new key for user "tony" (shown once)
  python3 vault/create_api_key.py tony --revoke   # remove all of tony's keys

Vault stores only a SHA-256 hash of each key (secret/agent/api-keys), mapped to its
user. The key itself is printed once here and never stored - put it in your
password manager. The API picks up changes within a minute.
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys

VAULT_CONTAINER = os.environ.get("VAULT_CONTAINER", "enterprise-vault")
PATH = "secret/agent/api-keys"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def vault(args, token, stdin=None):
    return subprocess.run(["docker", "exec", "-i", "-e", "VAULT_TOKEN", VAULT_CONTAINER, "vault", *args],
                          input=stdin, capture_output=True, env={**os.environ, "VAULT_TOKEN": token})


def main():
    parser = argparse.ArgumentParser(description="Create or revoke a chat API key")
    parser.add_argument("user", help="user ID the key acts as (letters, digits, . _ -)")
    parser.add_argument("--revoke", action="store_true", help="remove all keys for this user")
    args = parser.parse_args()
    if not ID_RE.fullmatch(args.user):
        sys.exit("[!] user must be 1-64 characters of letters, digits, '.', '_' or '-'")

    token = getpass.getpass("Vault root token: ")
    got = vault(["kv", "get", "-format=json", PATH], token)
    if got.returncode == 0:
        keys = json.loads(got.stdout)["data"]["data"]
    elif b"No value found" in got.stderr or b"not found" in got.stderr.lower():
        keys = {}
    else:
        sys.exit(f"[!] Could not read {PATH}: {got.stderr.decode()[:300]}")

    if args.revoke:
        before = len(keys)
        keys = {h: u for h, u in keys.items() if u != args.user}
        removed = before - len(keys)
        new_key = None
    else:
        new_key = "wa_" + secrets.token_urlsafe(32)
        keys[hashlib.sha256(new_key.encode()).hexdigest()] = args.user

    if keys:
        put = vault(["kv", "put", PATH, "-"], token, stdin=json.dumps(keys).encode())
    else:  # nothing left: delete the secret entirely
        put = vault(["kv", "metadata", "delete", PATH], token)
    if put.returncode != 0:
        sys.exit(f"[!] Vault write failed: {put.stderr.decode()[:300]}")

    if args.revoke:
        print(f"[*] Revoked {removed} key(s) for '{args.user}' (takes effect within a minute)")
    else:
        print(f"[*] API key for '{args.user}' (shown once - save it in your password manager):")
        print(f"\n    {new_key}\n")
        print("    Use it as:  -H \"X-API-Key: <key>\"  on https://api.ai.internal")


if __name__ == "__main__":
    main()
