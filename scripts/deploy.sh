#!/bin/bash
# deploy.sh — Deploy Kronos II to VPS
#
# Usage: deploy.sh [--first-run]
#
# Safe deploy: syncs code via rsync, preserves all config and state.
# NEVER use `git reset --hard` on VPS — this script is the only way to deploy.

set -euo pipefail

# Load env vars from .env if present (for KRONOS_VPS etc.)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

VPS="${KRONOS_VPS:?Set KRONOS_VPS=user@host in .env or environment}"
REMOTE_DIR="/opt/kronos-ii"
AGENTS="kronos-ii nexus lacuna keystone impulse resonant"

echo "=== Deploying Kronos II ==="

# Sync code — explicitly exclude everything that must survive deploy
echo "Syncing files..."
rsync -avz --delete \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.egg-info/' \
  --exclude='data/' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='*.session' \
  --exclude='.venv/' \
  --exclude='workspaces/' \
  "$(dirname "$0")/../" "$VPS:$REMOTE_DIR/app/"

if [ "${1:-}" = "--first-run" ]; then
  echo "First run setup..."
  ssh "$VPS" bash <<'REMOTE'
    set -euo pipefail
    cd /opt/kronos-ii

    # Create venv
    python3 -m venv app/.venv
    app/.venv/bin/pip install -e "app/.[dev]"
    app/.venv/bin/pip install edge-tts

    # Install systemd units (replace default User=kronos with actual VPS user)
    VPS_USER=$(whoami)
    for f in app/systemd/*.service app/systemd/*.timer; do
      sudo sed "s/User=kronos/User=$VPS_USER/" "$f" > /etc/systemd/system/$(basename "$f")
    done
    sudo systemctl daemon-reload

    echo "First run setup complete."
    echo "Next steps:"
    echo "  1. Create .env files from .env.example"
    echo "  2. Run auth-userbot.py for each agent"
    echo "  3. sudo systemctl enable --now kronos-ii nexus lacuna keystone impulse resonant"
REMOTE
else
  echo "Deploying to VPS..."
  ssh "$VPS" bash <<'REMOTE'
    set -euo pipefail
    cd /opt/kronos-ii

    # === Safety checks ===

    # Verify .env exists
    if [ ! -f app/.env ]; then
      echo "FATAL: app/.env not found! Aborting."
      exit 1
    fi

    # Verify session files exist for all agents
    MISSING_SESSIONS=""
    for agent in kronos nexus lacuna keystone impulse resonant; do
      if [ ! -f "app/${agent}.session" ]; then
        MISSING_SESSIONS="$MISSING_SESSIONS $agent"
      fi
    done
    if [ -n "$MISSING_SESSIONS" ]; then
      echo "WARNING: Missing session files:$MISSING_SESSIONS"
      echo "Run: AGENT_NAME=<name> .venv/bin/python scripts/auth-userbot.py"
    fi

    # Verify TG_BOT_TOKEN is NOT in agent-specific .env files
    for f in app/.env.nexus app/.env.lacuna app/.env.keystone app/.env.impulse app/.env.resonant; do
      if [ -f "$f" ] && grep -qP '^TG_BOT_TOKEN=.+' "$f" 2>/dev/null; then
        echo "WARNING: $f contains TG_BOT_TOKEN — agents should use userbot, not bot!"
      fi
    done

    # Update systemd units if changed (replace default User=kronos with actual VPS user)
    VPS_USER=$(whoami)
    for f in app/systemd/*.service app/systemd/*.timer; do
      [ -f "$f" ] && sudo sed "s/User=kronos/User=$VPS_USER/" "$f" > /etc/systemd/system/$(basename "$f")
    done
    sudo systemctl daemon-reload

    # Reinstall package (in case deps changed)
    app/.venv/bin/python -m pip install -e "app/." --quiet 2>/dev/null || true

    # Restart all agents
    echo "Restarting all agents..."
    sudo systemctl restart kronos-ii nexus lacuna keystone impulse resonant

    sleep 3

    # Verify all agents are running
    echo ""
    echo "Agent status:"
    for svc in kronos-ii nexus lacuna keystone impulse resonant; do
      STATUS=$(systemctl is-active $svc)
      echo "  $svc: $STATUS"
    done

    echo ""
    echo "Deploy complete."
REMOTE
fi

echo "=== Done ==="
