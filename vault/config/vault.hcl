# Vault server config for the enterprise-ai-stack (replaces dev mode).
# Data is kept on the vault_data Docker volume, so secrets survive restarts.
# After every Vault restart, run ./vault/unseal.sh on the host.

ui = true

storage "file" {
  path = "/vault/file"
}

# Plain HTTP is acceptable here only because port 8200 is NOT published to the
# host: just containers on the enterprise-ai-mesh network can reach Vault.
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

api_addr = "http://enterprise-vault:8200"

# Vault 2.x requires a token to start root-token generation. With no standing
# root token (we revoke it after admin work), allow it without one so
# ./vault/new_root.sh works; finishing still needs the unseal key.
# Only generate-root is opened; rekey and generate-operation-token stay closed.
enable_unauthenticated_access = ["generate-root"]
