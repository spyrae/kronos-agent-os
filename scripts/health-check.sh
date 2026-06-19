#!/bin/bash
# health-check.sh — Composite health check for Kronos Agent OS stack
# Checks: kaos systemd service, bridge health (port 8788), dashboard health (port 8789), disk, memory
#
# Usage: health-check.sh [--verbose] [--alert]
# Exit codes: 0 = all healthy, 1 = one or more components unhealthy
#
# Can be used with monitoring tools, cron, or manual verification.

set -uo pipefail

VERBOSE_MODE=false
ALERT_ENABLED=false
for arg in "$@"; do
  case "$arg" in
    --verbose|-v)
      VERBOSE_MODE=true
      ;;
    --alert)
      ALERT_ENABLED=true
      ;;
    -h|--help)
      echo "Usage: health-check.sh [--verbose|-v] [--alert]"
      exit 0
      ;;
    *)
      echo "Usage: health-check.sh [--verbose|-v] [--alert]" >&2
      echo "ERROR: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

# Resolve the install dir relative to this script so the checks work on any
# deployment path without hardcoding it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "$SCRIPT_DIR/_common.sh"
kaos_common_init
APP_DIR="$KAOS_APP_DIR"

# Main agent systemd unit name. Generic default; override via KAOS_MAIN_UNIT in
# the environment / .env (e.g. KAOS_MAIN_UNIT=kronos-ii) when the unit is named
# differently than the public default.
MAIN_UNIT="$KAOS_MAIN_UNIT_RESOLVED"

WEBHOOK_URL="${REMINDER_WEBHOOK_URL:-http://127.0.0.1:8788/webhook}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"

BRIDGE_HEALTH="http://127.0.0.1:8788/health"
DASHBOARD_HEALTH="http://127.0.0.1:8789/api/health"

# Disk/memory thresholds
DISK_WARN_PCT=85
MEM_WARN_PCT=90

errors=()
warnings=()

log() {
  if [ "$VERBOSE_MODE" = true ]; then
    echo "[$(date '+%H:%M:%S')] $*"
  fi
}

check_pass() {
  log "OK: $1"
}

check_fail() {
  log "FAIL: $1"
  errors+=("$1")
}

check_warn() {
  log "WARN: $1"
  warnings+=("$1")
}

send_notification() {
  local title="$1"
  local priority="$2"
  local tags="$3"
  local text="$4"

  # Send to Telegram bridge webhook.
  if [ -n "$WEBHOOK_SECRET" ]; then
    json=$(python3 -c "import json,sys; print(json.dumps({'text': sys.argv[1]}))" "$text" 2>/dev/null)
    curl -s -X POST "$WEBHOOK_URL" \
      -H "Content-Type: application/json" \
      -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
      -d "$json" > /dev/null 2>&1
  fi

  # Send to NTFY (phone push notification).
  if [ -n "$NTFY_TOKEN" ]; then
    curl -s -d "$text" \
      -H "Title: $title" \
      -H "Priority: $priority" \
      -H "Tags: $tags" \
      -H "Authorization: Bearer $NTFY_TOKEN" \
      "$NTFY_URL/$NTFY_TOPIC" > /dev/null 2>&1
  fi
}

bridge_response=$(curl -sf --max-time 5 "$BRIDGE_HEALTH" 2>/dev/null || true)
bridge_healthy=false
if echo "$bridge_response" | grep -q '"status".*"ok"'; then
  bridge_healthy=true
fi

# --- Check 1: main agent systemd service ---
service_active=$(systemctl is-active "$MAIN_UNIT" 2>/dev/null || true)
unit_load_state=$(systemctl show "$MAIN_UNIT" --property=LoadState --value 2>/dev/null | head -n1 || true)
if [ "$service_active" = "active" ]; then
  check_pass "$MAIN_UNIT service active"
else
  if [ "$unit_load_state" = "not-found" ] || { [ -z "$unit_load_state" ] && [ "$service_active" = "unknown" ]; }; then
    if [ "$bridge_healthy" = true ]; then
      check_warn "$MAIN_UNIT service not found/misconfigured, but bridge is healthy (set KAOS_MAIN_UNIT to this install's unit name)"
    else
      check_fail "$MAIN_UNIT service not found/misconfigured and bridge is not healthy"
    fi
  elif [ "$bridge_healthy" = true ]; then
    check_fail "$MAIN_UNIT service ${service_active:-unknown} while bridge is healthy (orphan bridge process or stale endpoint)"
  else
    check_fail "$MAIN_UNIT service: ${service_active:-unknown}"
  fi
fi

# --- Check 2: Bridge health endpoint (port 8788) ---
if [ "$bridge_healthy" = true ]; then
  check_pass "Bridge /health: OK"
else
  check_fail "Bridge /health not responding (port 8788)"
fi

# --- Check 3: Dashboard health endpoint (port 8789) ---
dashboard_response=$(curl -sf --max-time 5 "$DASHBOARD_HEALTH" 2>/dev/null)
if [ -n "$dashboard_response" ]; then
  check_pass "Dashboard /api/health: OK"
else
  check_fail "Dashboard /api/health not responding (port 8789)"
fi

# --- Check 4: heartbeat timer ---
heartbeat_active=$(systemctl is-active kronos-health.timer 2>/dev/null)
if [ "$heartbeat_active" = "active" ]; then
  check_pass "kronos-health.timer active"
else
  check_warn "kronos-health.timer: ${heartbeat_active:-unknown}"
fi

# --- Check 5: Disk usage ---
disk_pct=$(df / 2>/dev/null | awk 'NR==2 {print $5}' | sed 's/%//')
if [ -n "$disk_pct" ]; then
  if [ "$disk_pct" -ge "$DISK_WARN_PCT" ] 2>/dev/null; then
    check_fail "Disk usage critical: ${disk_pct}% (threshold: ${DISK_WARN_PCT}%)"
  else
    check_pass "Disk usage: ${disk_pct}%"
  fi
else
  check_warn "Could not read disk usage"
fi

# --- Check 6: Memory usage ---
if command -v free > /dev/null 2>&1; then
  mem_total=$(free 2>/dev/null | awk 'NR==2 {print $2}')
  mem_used=$(free 2>/dev/null | awk 'NR==2 {print $3}')
  if [ -n "$mem_total" ] && [ "$mem_total" -gt 0 ] 2>/dev/null; then
    mem_pct=$(( mem_used * 100 / mem_total ))
    if [ "$mem_pct" -ge "$MEM_WARN_PCT" ] 2>/dev/null; then
      check_fail "Memory usage critical: ${mem_pct}% (threshold: ${MEM_WARN_PCT}%)"
    else
      check_pass "Memory usage: ${mem_pct}%"
    fi
  else
    check_warn "Could not read memory usage"
  fi
fi

# --- Summary ---
total_checks=6
failed=${#errors[@]}
warned=${#warnings[@]}
passed=$((total_checks - failed - warned))

if [ "$VERBOSE_MODE" = true ]; then
  echo ""
  echo "=== Health Summary ==="
  echo "Passed: $passed/$total_checks"
  [ "$warned" -gt 0 ] && echo "Warnings: $warned"
  [ "$failed" -gt 0 ] && echo "Failed: $failed"
  echo ""
fi

# --- Alert if requested ---
if [ "$ALERT_ENABLED" = true ] && [ "$warned" -gt 0 ]; then
  warning_text=$(printf '⚠️ Kronos Agent OS Health Warning\n\nWarnings (%d/%d):\n' "$warned" "$total_checks")
  for warn in "${warnings[@]}"; do
    warning_text+="- $warn"$'\n'
  done
  send_notification "Kronos Agent OS Health Warning" "low" "warning" "$warning_text"
fi

if [ "$VERBOSE_MODE" != true ] && [ "$warned" -gt 0 ]; then
  for warn in "${warnings[@]}"; do
    echo "WARN: $warn"
  done
fi

if [ "$failed" -gt 0 ]; then
  if [ "$ALERT_ENABLED" = true ]; then
    alert_text=$(printf '🚨 Kronos Agent OS Health Alert\n\nFailed checks (%d/%d):\n' "$failed" "$total_checks")
    for err in "${errors[@]}"; do
      alert_text+="- $err"$'\n'
    done
    send_notification "Kronos Agent OS Health Alert" "urgent" "rotating_light,skull" "$alert_text"
  fi

  # Print errors for non-verbose mode too
  if [ "$VERBOSE_MODE" != true ]; then
    for err in "${errors[@]}"; do
      echo "FAIL: $err"
    done
  fi

  exit 1
fi

if [ "$VERBOSE_MODE" = true ]; then
  echo "All checks passed."
fi

exit 0
