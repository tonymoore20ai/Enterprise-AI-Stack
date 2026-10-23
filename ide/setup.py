#!/usr/bin/env python3
"""
One-time IDE setup: your user ID (so files you create in the IDE belong to you)
and the IDE login password (stored only as an argon2 hash).
Run on the host from the project folder:

  python3 ide/setup.py

Writes to .env (replacing earlier values): HOST_UID, HOST_GID, CODE_SERVER_HASHED_PASSWORD.
The password is hashed inside a throwaway python:3.12-slim container and is never
written anywhere in plain text. Re-run any time to change the password.
"""
import getpass
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")

HASH_CMD = ("pip install -q --disable-pip-version-check --root-user-action=ignore argon2-cffi "
            ">/dev/null 2>&1 && python -c 'import sys; from argon2 import PasswordHasher; "
            "print(PasswordHasher().hash(sys.stdin.read()))'")


def set_vars(values: dict):
    text = open(ENV).read() if os.path.exists(ENV) else ""
    for k, v in values.items():
        line = f"{k}='{v}'"
        if re.search(rf"^{k}=", text, re.M):
            text = re.sub(rf"^{k}=.*$", lambda _: line, text, flags=re.M)
        else:
            if "# IDE (added by ide/setup.py)" not in text:
                text = text.rstrip("\n") + "\n\n# IDE (added by ide/setup.py)\n"
            text += line + "\n"
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(ENV, 0o600)


def main():
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        sys.exit("[!] Run this as your normal user, not with sudo.")
    pw = getpass.getpass("New IDE password (12+ characters): ")
    if len(pw) < 12:
        sys.exit("[!] Use at least 12 characters - this password protects a terminal.")
    if getpass.getpass("Repeat it: ") != pw:
        sys.exit("[!] The passwords don't match.")

    print("[*] Hashing (pulls python:3.12-slim the first time)...")
    proc = subprocess.run(["docker", "run", "--rm", "-i", "python:3.12-slim", "sh", "-c", HASH_CMD],
                          input=pw.encode(), capture_output=True)
    hashed = proc.stdout.decode().strip()
    if proc.returncode != 0 or not hashed.startswith("$argon2"):
        sys.exit(f"[!] Hashing failed: {proc.stderr.decode()[-300:]}")

    set_vars({"HOST_UID": uid, "HOST_GID": gid, "CODE_SERVER_HASHED_PASSWORD": hashed})
    print(f"[*] Saved to .env (mode 600): HOST_UID={uid}, HOST_GID={gid}, CODE_SERVER_HASHED_PASSWORD")


if __name__ == "__main__":
    main()
