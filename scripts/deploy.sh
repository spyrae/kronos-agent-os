#!/bin/bash
# deploy.sh — Deploy Kronos Agent OS to a remote host
#
# Usage: deploy.sh [--first-run]
#
# Safe deploy: syncs code via rsync, preserves all config and state.
# NEVER use `git reset --hard` on the remote host. This script is the only
# deployment path because it preserves local config and runtime state.

set -euo pipefail

# Load env vars from .env if present (for KAOS_REMOTE etc.)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

REMOTE="${KAOS_REMOTE:?Set KAOS_REMOTE=user@host in .env or environment}"
REMOTE_DIR="${KAOS_REMOTE_DIR:-/opt/kaos}"
AGENTS="${KAOS_AGENTS:-kaos}"

echo "=== Deploying Kronos Agent OS ==="

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
  "$(dirname "$0")/../" "$REMOTE:$REMOTE_DIR/app/"

if [ "${1:-}" = "--first-run" ]; then
  echo "First run setup..."
  ssh "$REMOTE" "KAOS_REMOTE_DIR='$REMOTE_DIR' bash -s" <<'REMOTE_SCRIPT'
    set -euo pipefail
    cd "$KAOS_REMOTE_DIR"

    # Create venv
    python3 -m venv app/.venv
    app/.venv/bin/pip install -e "app/.[dev]"
    app/.venv/bin/pip install edge-tts

    # Install systemd units (replace default User=kronos with actual remote user)
    REMOTE_USER=$(whoami)
    for f in app/systemd/*.service app/systemd/*.timer; do
      sudo sed "s/User=kronos/User=$REMOTE_USER/" "$f" > /etc/systemd/system/$(basename "$f")
    done
    sudo systemctl daemon-reload

    echo "First run setup complete."
    echo "Next steps:"
    echo "  1. Create .env files from .env.example"
    echo "  2. Run auth-userbot.py for each agent"
    echo "  3. sudo systemctl enable --now kaos"
REMOTE_SCRIPT
else
  echo "Deploying to remote host..."
  ssh "$REMOTE" "KAOS_REMOTE_DIR='$REMOTE_DIR' KAOS_AGENTS='$AGENTS' bash -s" <<'REMOTE_SCRIPT'
    set -euo pipefail
    cd "$KAOS_REMOTE_DIR"

    # === Safety checks ===

    # Verify .env exists
    if [ ! -f app/.env ]; then
      echo "FATAL: app/.env not found! Aborting."
      exit 1
    fi

    # Verify session files exist for all agents
    MISSING_SESSIONS=""
    for agent in $KAOS_AGENTS; do
      if [ ! -f "app/${agent}.session" ]; then
        MISSING_SESSIONS="$MISSING_SESSIONS $agent"
      fi
    done
    if [ -n "$MISSING_SESSIONS" ]; then
      echo "WARNING: Missing session files:$MISSING_SESSIONS"
      echo "Run: AGENT_NAME=<name> .venv/bin/python scripts/auth-userbot.py"
    fi

    # Verify TG_BOT_TOKEN is NOT in agent-specific .env files
    for agent in $KAOS_AGENTS; do
      f="app/.env.$agent"
      if [ -f "$f" ] && grep -qP '^TG_BOT_TOKEN=.+' "$f" 2>/dev/null; then
        echo "WARNING: $f contains TG_BOT_TOKEN — agents should use userbot, not bot!"
      fi
    done

    # Update systemd units if changed (replace default User=kronos with actual remote user)
    REMOTE_USER=$(whoami)
    for f in app/systemd/*.service app/systemd/*.timer; do
      [ -f "$f" ] && sudo sed "s/User=kronos/User=$REMOTE_USER/" "$f" > /etc/systemd/system/$(basename "$f")
    done
    sudo systemctl daemon-reload

    # Reinstall package (in case deps changed)
    app/.venv/bin/python -m pip install -e "app/." --quiet 2>/dev/null || true

    # Restart all agents
    echo "Restarting all agents..."
    sudo systemctl restart $KAOS_AGENTS

    sleep 3

    # Verify all agents are running
    echo ""
    echo "Agent status:"
    for svc in $KAOS_AGENTS; do
      STATUS=$(systemctl is-active $svc)
      echo "  $svc: $STATUS"
    done

    echo ""
    echo "Deploy complete."
REMOTE_SCRIPT
fi

echo "=== Done ==="
