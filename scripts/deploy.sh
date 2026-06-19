#!/bin/bash
# deploy.sh — Deploy Kronos Agent OS to a remote host or local runner host
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
    while IFS='=' read -r key value || [ -n "$key" ]; do
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        case "$key" in
          ''|\#*) continue ;;
        esac
        if [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < "$ENV_FILE"
fi

DEPLOY_MODE="${KAOS_DEPLOY_MODE:-remote}"
REMOTE_DIR="${KAOS_REMOTE_DIR:-/opt/kaos}"
AGENTS="${KAOS_AGENTS:-kaos}"
# systemd unit names used for restart/status. These may differ from the
# agent_name list in KAOS_AGENTS (e.g. the main kronos agent runs as the
# `kronos-ii` unit). Defaults to KAOS_AGENTS when not set.
SERVICES="${KAOS_SERVICES:-$AGENTS}"
MAIN_UNIT="${KAOS_MAIN_UNIT:-${SERVICES%% *}}"
# Whether deploy installs app/systemd/* units into /etc/systemd/system. The
# units in app/systemd/ are generic: they use the /opt/kaos placeholder path
# and User=kronos. On install the deploy rewrites /opt/kaos -> KAOS_REMOTE_DIR
# and User=kronos -> the remote user, so the public units land correctly on any
# install dir (incl. /opt/kronos-ii). Set false only when units are fully
# provisioned outside the deploy (e.g. hand-managed per-agent named units).
MANAGE_SYSTEMD="${KAOS_MANAGE_SYSTEMD:-true}"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "FATAL: $*" >&2
  exit 1
}

validate_remote_dir() {
  if [ -z "$REMOTE_DIR" ]; then
    fail "KAOS_REMOTE_DIR must not be empty. Example: /opt/kronos-ii"
  fi
  if [[ "$REMOTE_DIR" != /* ]]; then
    fail "KAOS_REMOTE_DIR must be an absolute path. Example: /opt/kronos-ii"
  fi
  if [[ ! "$REMOTE_DIR" =~ ^/[A-Za-z0-9/_.-]+$ ]]; then
    fail "KAOS_REMOTE_DIR contains unsafe characters for systemd template rewrite: '$REMOTE_DIR'. Allowed: [A-Za-z0-9/_.-], example: /opt/kronos-ii"
  fi
}

if [ "$DEPLOY_MODE" != "local" ] && [ "$DEPLOY_MODE" != "remote" ]; then
  fail "KAOS_DEPLOY_MODE must be 'local' or 'remote'."
fi

validate_remote_dir

if [ "$DEPLOY_MODE" = "remote" ]; then
  REMOTE="${KAOS_REMOTE:?Set KAOS_REMOTE=user@host in .env or environment}"
else
  REMOTE=""
fi

echo "=== Deploying Kronos Agent OS ==="
echo "Mode: $DEPLOY_MODE"
echo "Target dir: $REMOTE_DIR"
echo "Agents: $AGENTS"
echo "Services: $SERVICES"
echo "Main unit: $MAIN_UNIT"
echo "Manage systemd: $MANAGE_SYSTEMD"

sync_files() {
  local target

  if [ "$DEPLOY_MODE" = "local" ]; then
    sudo mkdir -p "$REMOTE_DIR/app"
    sudo chown -R "$(id -un):$(id -gn)" "$REMOTE_DIR"
    target="$REMOTE_DIR/app/"
  else
    target="$REMOTE:$REMOTE_DIR/app/"
  fi

  # Sync code — explicitly exclude everything that must survive deploy.
  echo "Syncing files..."
  rsync -avz --delete \
    --exclude='.DS_Store' \
    --exclude='.git/' \
    --exclude='.codegraph/' \
    --exclude='.pytest_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='__pycache__/' \
    --exclude='*.egg-info/' \
    --exclude='build/' \
    --exclude='dist/' \
    --exclude='mcp-server.log' \
    --exclude='node_modules/' \
    --exclude='data/' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='*.session' \
    --exclude='*.session-*' \
    --exclude='.venv/' \
    --exclude='workspaces/' \
    "$SOURCE_DIR/" "$target"
}

target_bash() {
  if [ "$DEPLOY_MODE" = "local" ]; then
    KAOS_REMOTE_DIR="$REMOTE_DIR" \
    KAOS_AGENTS="$AGENTS" \
    KAOS_SERVICES="$SERVICES" \
    KAOS_MAIN_UNIT="$MAIN_UNIT" \
    KAOS_MANAGE_SYSTEMD="$MANAGE_SYSTEMD" \
    KAOS_HEALTH_URL="${KAOS_HEALTH_URL:-}" \
    KAOS_HEALTH_REQUIRED="${KAOS_HEALTH_REQUIRED:-true}" \
    bash -s
  else
    ssh "$REMOTE" "KAOS_REMOTE_DIR='$REMOTE_DIR' KAOS_AGENTS='$AGENTS' KAOS_SERVICES='$SERVICES' KAOS_MAIN_UNIT='$MAIN_UNIT' KAOS_MANAGE_SYSTEMD='$MANAGE_SYSTEMD' KAOS_HEALTH_URL='${KAOS_HEALTH_URL:-}' KAOS_HEALTH_REQUIRED='${KAOS_HEALTH_REQUIRED:-true}' bash -s"
  fi
}

sync_files

if [ "${1:-}" = "--first-run" ]; then
  echo "First run setup..."
  target_bash <<'TARGET_SCRIPT'
    set -euo pipefail
    cd "$KAOS_REMOTE_DIR"

    if ! python3 - <<'PY' >/dev/null 2>&1
import ensurepip
PY
    then
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
      else
        echo "FATAL: python3 venv/ensurepip is unavailable and apt-get was not found."
        exit 1
      fi
    fi

    if [ -d app/.venv ] && [ ! -x app/.venv/bin/pip ]; then
      echo "Removing incomplete virtualenv from previous failed setup."
      rm -rf app/.venv
    fi

    # Create venv
    python3 -m venv app/.venv
    app/.venv/bin/pip install -e "app/.[dev]"
    app/.venv/bin/pip install edge-tts

    # Install systemd units: rewrite the generic /opt/kaos placeholder to this
    # install's KAOS_REMOTE_DIR and the User=kronos placeholder to the remote user.
    # Also rewrite ops dependencies from the public default kaos.service to the
    # real main unit for renamed installs.
    # Skipped when KAOS_MANAGE_SYSTEMD=false, matching the update deploy branch.
    if [ "${KAOS_MANAGE_SYSTEMD:-true}" = "true" ]; then
      should_install_systemd_unit() {
        local unit_name="$1"
        if [ "$unit_name" != "kaos.service" ]; then
          return 0
        fi
        for svc in ${KAOS_SERVICES:-$KAOS_AGENTS}; do
          if [ "$svc" = "kaos" ]; then
            return 0
          fi
        done
        echo "Skipping kaos.service install (KAOS_SERVICES does not include kaos; main unit: $KAOS_MAIN_UNIT)."
        return 1
      }

      REMOTE_USER=$(whoami)
      for f in app/systemd/*.service app/systemd/*.timer; do
        [ -f "$f" ] || continue
        unit_name="$(basename "$f")"
        should_install_systemd_unit "$unit_name" || continue
        sudo sed \
          -e "s/User=kronos/User=$REMOTE_USER/" \
          -e "s|/opt/kaos|$KAOS_REMOTE_DIR|g" \
          -e "s/After=kaos.service/After=$KAOS_MAIN_UNIT/g" \
          "$f" | sudo tee "/etc/systemd/system/$unit_name" >/dev/null
      done
      sudo systemctl daemon-reload
    else
      echo "Skipping systemd unit install (KAOS_MANAGE_SYSTEMD=false)."
    fi

    echo "First run setup complete."
    echo "Next steps:"
    echo "  1. Create .env files from .env.example"
    echo "  2. Run auth-userbot.py for each agent"
    echo "  3. sudo systemctl enable --now kaos"
TARGET_SCRIPT
else
  echo "Deploying to target host..."
  target_bash <<'TARGET_SCRIPT'
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

    # Install/update systemd units: rewrite the generic /opt/kaos placeholder to
    # this install's KAOS_REMOTE_DIR and the User=kronos placeholder to the
    # remote user. The generic public units (kaos.service, ops .service/.timer)
    # thus land with correct absolute paths on any install dir. Hand-managed
    # per-agent named units (not in the repo) are left untouched.
    # Ops service dependencies are rewritten from After=kaos.service to the
    # install's real KAOS_MAIN_UNIT, so timers are ordered correctly on renamed
    # installs.
    # Generic kaos.service is installed only when KAOS_SERVICES includes kaos.
    # Skipped when KAOS_MANAGE_SYSTEMD=false.
    if [ "${KAOS_MANAGE_SYSTEMD:-true}" = "true" ]; then
      should_install_systemd_unit() {
        local unit_name="$1"
        if [ "$unit_name" != "kaos.service" ]; then
          return 0
        fi
        for svc in ${KAOS_SERVICES:-$KAOS_AGENTS}; do
          if [ "$svc" = "kaos" ]; then
            return 0
          fi
        done
        echo "Skipping kaos.service install (KAOS_SERVICES does not include kaos; main unit: $KAOS_MAIN_UNIT)."
        return 1
      }

      REMOTE_USER=$(whoami)
      for f in app/systemd/*.service app/systemd/*.timer; do
        [ -f "$f" ] || continue
        unit_name="$(basename "$f")"
        should_install_systemd_unit "$unit_name" || continue
        sudo sed \
          -e "s/User=kronos/User=$REMOTE_USER/" \
          -e "s|/opt/kaos|$KAOS_REMOTE_DIR|g" \
          -e "s/After=kaos.service/After=$KAOS_MAIN_UNIT/g" \
          "$f" | sudo tee "/etc/systemd/system/$unit_name" >/dev/null
      done
      sudo systemctl daemon-reload
    else
      echo "Skipping systemd unit install (KAOS_MANAGE_SYSTEMD=false)."
    fi

    # Reinstall package (in case deps changed)
    app/.venv/bin/python -m pip install -e "app/." --quiet 2>/dev/null || true

    # Restart all agents (systemd unit names from KAOS_SERVICES, which may
    # differ from the agent_name list used for the safety checks above).
    echo "Restarting all agents..."
    sudo systemctl restart ${KAOS_SERVICES:-$KAOS_AGENTS}

    sleep 3

    # Verify all agents are running
    echo ""
    echo "Agent status:"
    for svc in ${KAOS_SERVICES:-$KAOS_AGENTS}; do
      if ! STATUS=$(systemctl is-active "$svc"); then
        echo "  $svc: $STATUS"
        echo ""
        echo "Last logs for $svc:"
        journalctl -u "$svc" -n 80 --no-pager || true
        exit 1
      fi
      echo "  $svc: $STATUS"
    done

    if [ -n "${KAOS_HEALTH_URL:-}" ]; then
      echo ""
      echo "Health check: $KAOS_HEALTH_URL"
      HEALTH_OK=false
      for attempt in {1..12}; do
        if curl -fsS --max-time 10 "$KAOS_HEALTH_URL"; then
          HEALTH_OK=true
          break
        fi
        if [ "$attempt" -lt 12 ]; then
          sleep 3
        fi
      done
      if [ "$HEALTH_OK" != "true" ]; then
        echo ""
        echo "Health check failed: $KAOS_HEALTH_URL"
        if [ "${KAOS_HEALTH_REQUIRED:-true}" = "true" ]; then
          exit 1
        fi
      fi
      echo ""
    fi

    echo ""
    echo "Deploy complete."
TARGET_SCRIPT
fi

echo "=== Done ==="
