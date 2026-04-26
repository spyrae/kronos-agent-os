#!/bin/bash
# Setup .env files for all agents on VPS.
# Run once on the server after initial deploy.
#
# Usage: cd /opt/kronos-ii/app && bash scripts/setup-agents.sh
#
# Prerequisites:
#   - .env (Kronos) already exists with shared keys (LLM, MCP, etc.)
#   - Telegram API credentials for each agent

set -euo pipefail

APP_DIR="/opt/kronos-ii/app"
BASE_ENV="$APP_DIR/.env"

if [ ! -f "$BASE_ENV" ]; then
    echo "ERROR: $BASE_ENV not found. Set up Kronos first."
    exit 1
fi

# Extract shared keys from base .env (exclude agent-specific vars)
extract_shared() {
    grep -v -E '^(TG_API_ID|TG_API_HASH|TG_BOT_TOKEN|AGENT_NAME|WEBHOOK_PORT|SESSION_FILE|DEFAULT_NOTIFY_CHAT|WORKSPACE_PATH|DB_PATH)' "$BASE_ENV" | grep -v '^#' | grep -v '^$'
}

create_env() {
    local agent=$1
    local api_id=$2
    local api_hash=$3
    local webhook_port=$4
    local env_file="$APP_DIR/.env.$agent"

    if [ -f "$env_file" ]; then
        echo "SKIP: $env_file already exists"
        return
    fi

    echo "Creating $env_file..."
    {
        echo "# === $agent ==="
        echo "AGENT_NAME=$agent"
        echo "TG_API_ID=$api_id"
        echo "TG_API_HASH=$api_hash"
        echo "WEBHOOK_PORT=$webhook_port"
        echo ""
        extract_shared
    } > "$env_file"

    chmod 600 "$env_file"
    echo "OK: $env_file"
}

# Agent credentials: name, api_id, api_hash, webhook_port
# Each agent needs its own Telegram API credentials (https://my.telegram.org)
# Set them via environment variables before running this script:
#   export NEXUS_API_ID=... NEXUS_API_HASH=...
create_env "nexus"    "${NEXUS_API_ID:?Set NEXUS_API_ID}"       "${NEXUS_API_HASH:?Set NEXUS_API_HASH}"       8789
create_env "lacuna"   "${LACUNA_API_ID:?Set LACUNA_API_ID}"     "${LACUNA_API_HASH:?Set LACUNA_API_HASH}"     8790
create_env "resonant" "${RESONANT_API_ID:?Set RESONANT_API_ID}" "${RESONANT_API_HASH:?Set RESONANT_API_HASH}" 8791
create_env "keystone" "${KEYSTONE_API_ID:?Set KEYSTONE_API_ID}" "${KEYSTONE_API_HASH:?Set KEYSTONE_API_HASH}" 8792
create_env "impulse"  "${IMPULSE_API_ID:?Set IMPULSE_API_ID}"   "${IMPULSE_API_HASH:?Set IMPULSE_API_HASH}"   8793

echo ""
echo "Next steps:"
echo "  1. Verify each .env file has correct shared keys"
echo "  2. Auth each agent's Telegram session:"
echo "     AGENT_NAME=lacuna TG_API_ID=... TG_API_HASH=... .venv/bin/python scripts/auth-userbot.py"
echo "  3. Install systemd units:"
echo "     cp systemd/{lacuna,resonant,keystone,impulse}.service /etc/systemd/system/"
echo "     systemctl daemon-reload"
echo "  4. Start agents:"
echo "     systemctl enable --now lacuna resonant keystone impulse"
