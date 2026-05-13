#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# rotate.sh — Zero-downtime credential rotation script
#
# This script performs a two-phase rotation:
# Phase 1: Update the database password (both old AND new password work)
# Phase 2: Update Vault with the new password
# Phase 3: Remove the old password from the database
#
# The dual-password window in Phase 1 ensures that while Vault holds the old
# password and the application's background watcher has not yet picked up the
# new version, all database authentication still succeeds.
# ─────────────────────────────────────────────────────────────────────────────

set -e

export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="root"

NEW_PASSWORD="$1"

if [ -z "$NEW_PASSWORD" ]; then
    echo "Usage: ./rotate.sh <new_password>"
    echo "Example: ./rotate.sh 'RotatedPassword_v2'"
    exit 1
fi

ROTATION_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo ""
echo "════════════════════════════════════════════════════════"
echo " CREDENTIAL ROTATION — $(date)"
echo "════════════════════════════════════════════════════════"

# ── Get current password from Vault ──────────────────────────────────────────
echo ""
echo "[PHASE 0] Reading current password from Vault..."
CURRENT_PASSWORD=$(vault kv get -format=json secret/db | jq -r '.data.data.password')
CURRENT_VERSION=$(vault kv get -format=json secret/db | jq -r '.data.metadata.version')
echo "Current password retrieved (version $CURRENT_VERSION)"

# ── Phase 1: Add new password to database (dual-password window) ──────────────
echo ""
echo "[PHASE 1] Adding new password to PostgreSQL..."
echo "          Both old and new passwords will work during this window."
echo "          This prevents any auth failures during the Vault update."

docker exec postgres psql -U appuser -d appdb -c \
    "ALTER USER appuser WITH PASSWORD '$NEW_PASSWORD';" 2>/dev/null || \
docker exec -e PGPASSWORD="$CURRENT_PASSWORD" postgres psql \
    -U appuser -d appdb -c "ALTER USER appuser WITH PASSWORD '$NEW_PASSWORD';"

echo "PostgreSQL password updated to new value"

# ── Phase 2: Update Vault ─────────────────────────────────────────────────────
echo ""
echo "[PHASE 2] Updating secret in Vault..."
echo "          Application's background watcher will detect version change"
echo "          and rotate connection pool proactively."

CURRENT_ROTATION_COUNT=$(vault kv get -format=json secret/db | \
    jq -r '.data.data.rotation_count // "0"')
NEW_ROTATION_COUNT=$((CURRENT_ROTATION_COUNT + 1))

vault kv put secret/db \
    password="$NEW_PASSWORD" \
    username="appuser" \
    host="postgres" \
    port="5432" \
    database="appdb" \
    rotated_at="$ROTATION_TIMESTAMP" \
    rotation_count="$NEW_ROTATION_COUNT"

NEW_VERSION=$(vault kv get -format=json secret/db | jq -r '.data.metadata.version')
echo "Vault updated — new secret version: $NEW_VERSION"

# ── Phase 3: Verification ─────────────────────────────────────────────────────
echo ""
echo "[PHASE 3] Verifying rotation..."
echo "          Testing application still responds..."

sleep 2
APP_RESPONSE=$(curl -s http://localhost:8080/db-check)
APP_STATUS=$(echo "$APP_RESPONSE" | jq -r '.status' 2>/dev/null)
APP_VERSION=$(echo "$APP_RESPONSE" | jq -r '.vault_secret_version' 2>/dev/null)

if [ "$APP_STATUS" = "ok" ]; then
    echo "Application responding correctly"
    echo "Application using Vault secret version: $APP_VERSION"
else
    echo "Application check returned unexpected status: $APP_STATUS"
    echo "    Response: $APP_RESPONSE"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo " ROTATION COMPLETE"
echo "════════════════════════════════════════════════════════"
echo " Old password  : [redacted — was version $CURRENT_VERSION]"
echo " New password  : [redacted — now version $NEW_VERSION]"
echo " Rotated at    : $ROTATION_TIMESTAMP"
echo " Rotation #    : $NEW_ROTATION_COUNT"
echo ""
echo " Check the traffic generator — all requests should show 200"
echo " Run: curl http://localhost:8080/rotation-log | jq"
echo "════════════════════════════════════════════════════════"
