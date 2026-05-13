#!/bin/bash

set -e

export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="root"

echo "[*] Waiting for Vault..."
for i in {1..30}; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$VAULT_ADDR/v1/sys/health" 2>/dev/null)
    if [[ "$STATUS" =~ ^(200|429|472|473|501|503)$ ]]; then
        echo "[✓] Vault is responding (HTTP $STATUS)"
        break
    fi
    echo "    Attempt $i/30..."
    sleep 2
done

echo ""
echo "[*] Enabling KV v2 secrets engine..."
vault secrets enable -path=secret kv-v2 2>/dev/null || echo "    (already enabled)"

echo ""
echo "[*] Writing initial database secret (version 1)..."
vault kv put secret/db \
    password="InitialPassword_v1" \
    username="appuser" \
    host="postgres" \
    port="5432" \
    database="appdb" \
    rotated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    rotation_count="0"

echo ""
echo "[*] Verifying secret was written..."
vault kv get secret/db

echo ""
echo "[*] Creating read-only policy for application..."
vault policy write app-readonly - << 'POLICY'
path "secret/data/db" {
  capabilities = ["read"]
}
path "secret/metadata/db" {
  capabilities = ["read"]
}
POLICY

echo ""
echo "[✓] Vault initialization complete"
echo ""
echo "════════════════════════════════════════════════════════"
echo " Secret path    : secret/db"
echo " Initial password: InitialPassword_v1"
echo " Postgres password must also be: InitialPassword_v1"
echo "════════════════════════════════════════════════════════"
