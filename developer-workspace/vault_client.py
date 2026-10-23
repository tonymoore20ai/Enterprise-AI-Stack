"""
Vault access for the agent: AppRole login + KV v2 reads, using plain `requests`
(no extra package, no image rebuild).

The AppRole credentials come from vault-approle-secrets/ on the host, mounted
read-only at /var/run/secrets/vault in agent-engine. They are created by
vault/bootstrap.sh.

  from vault_client import load_langfuse_env, read_secret
  load_langfuse_env()                 # sets LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
  keys = read_secret("agent/langfuse")
"""
import os

import requests

VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://enterprise-vault:8200").rstrip("/")
APPROLE_DIR = os.environ.get("VAULT_APPROLE_DIR", "/var/run/secrets/vault")
KV_MOUNT = "secret"

_token = None


class VaultError(RuntimeError):
    pass


def _read_cred(name: str) -> str:
    path = os.path.join(APPROLE_DIR, name)
    try:
        with open(path) as f:
            value = f.read().strip()
    except FileNotFoundError:
        raise VaultError(f"{path} not found. Run ./vault/bootstrap.sh on the host.") from None
    if not value:
        raise VaultError(f"{path} is empty. Re-run ./vault/bootstrap.sh on the host.")
    return value


def _check(resp: requests.Response, what: str) -> dict:
    if resp.status_code == 503:
        raise VaultError("Vault is sealed. Run ./vault/unseal.sh on the host.")
    if resp.status_code in (400, 403) and what == "login":
        raise VaultError("Vault rejected the AppRole login. Re-run ./vault/bootstrap.sh "
                         "to issue fresh credentials.")
    if resp.status_code == 403:
        raise VaultError(f"Vault denied access to {what} (check the agent-read policy).")
    if resp.status_code == 404:
        raise VaultError(f"{what} not found in Vault. Run ./vault/bootstrap.sh on the host.")
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise VaultError(f"Vault error on {what}: HTTP {resp.status_code}") from e
    return resp.json()


def login() -> str:
    """Log in with AppRole once per process and return the client token."""
    global _token
    if _token:
        return _token
    try:
        resp = requests.post(
            f"{VAULT_ADDR}/v1/auth/approle/login",
            json={"role_id": _read_cred("role_id"), "secret_id": _read_cred("secret_id")},
            timeout=5,
        )
    except requests.ConnectionError as e:
        raise VaultError(f"Cannot reach Vault at {VAULT_ADDR}. Is enterprise-vault running?") from e
    _token = _check(resp, "login")["auth"]["client_token"]
    return _token


def read_secret(path: str) -> dict:
    """Read a KV v2 secret, e.g. read_secret("agent/langfuse") -> {"public_key": ..., ...}."""
    global _token

    def _get():
        return requests.get(f"{VAULT_ADDR}/v1/{KV_MOUNT}/data/{path}",
                            headers={"X-Vault-Token": login()}, timeout=5)

    resp = _get()
    if resp.status_code == 403 and _token:
        # AppRole tokens expire after 1 h; long-running services (agent-api) log in again
        _token = None
        resp = _get()
    return _check(resp, f"{KV_MOUNT}/{path}")["data"]["data"]


def load_litellm_keys() -> dict:
    """The gateway's virtual keys from Vault (created by litellm/create_keys.py).
    Also exports the guardrails key as LITELLM_RAILS_KEY, which guardrails/config.yml
    names in api_key_env_var. Returns {"agent_key": ..., "rails_key": ...}."""
    keys = read_secret("agent/litellm")
    os.environ["LITELLM_RAILS_KEY"] = keys["rails_key"]
    return keys


def load_langfuse_env() -> None:
    """Put the Langfuse API keys from Vault into the environment.
    Must run before langfuse.get_client() is first called."""
    keys = read_secret("agent/langfuse")
    os.environ["LANGFUSE_PUBLIC_KEY"] = keys["public_key"]
    os.environ["LANGFUSE_SECRET_KEY"] = keys["secret_key"]
