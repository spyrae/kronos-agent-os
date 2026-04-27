#!/bin/bash
# health-check.sh — Composite health check for Kronos Agent OS stack
# Checks: kaos systemd service, bridge health (port 8788), dashboard health (port 8789), disk, memory
#
# Usage: health-check.sh [--verbose] [--alert]
# Exit codes: 0 = all healthy, 1 = one or more components unhealthy
#
# Can be used with monitoring tools, cron, or manual verification.

set -uo pipefail

VERBOSE="${1:-}"
ALERT="${2:-}"

WEBHOOK_URL="${REMINDER_WEBHOOK_URL:-http://127.0.0.1:8788/webhook}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"

# NTFY config (loaded from .env if available)
if [ -f /opt/kaos/app/.env ]; then
  # shellcheck disable=SC1091
  source /opt/kaos/app/.env 2>/dev/null || true
fi
NTFY_URL="${NTFY_URL:-${NTFY_URL:-https://ntfy.sh}}"
NTFY_TOKEN="${NTFY_TOKEN:-}"
NTFY_TOPIC="${NTFY_TOPIC:-persona-alerts}"

BRIDGE_HEALTH="http://127.0.0.1:8788/health"
DASHBOARD_HEALTH="http://127.0.0.1:8789/api/health"

# Disk/memory thresholds
DISK_WARN_PCT=85
MEM_WARN_PCT=90

errors=()
warnings=()

log() {
  if [ "$VERBOSE" = "--verbose" ] || [ "$VERBOSE" = "-v" ]; then
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

# --- Check 1: kaos systemd service ---
service_active=$(systemctl is-active kaos 2>/dev/null)
if [ "$service_active" = "active" ]; then
  check_pass "kaos service active"
else
  check_fail "kaos service: ${service_active:-unknown}"
fi

# --- Check 2: Bridge health endpoint (port 8788) ---
bridge_response=$(curl -sf --max-time 5 "$BRIDGE_HEALTH" 2>/dev/null)
if echo "$bridge_response" | grep -q '"status".*"ok"'; then
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
passed=$((total_checks - failed))

if [ "$VERBOSE" = "--verbose" ] || [ "$VERBOSE" = "-v" ]; then
  echo ""
  echo "=== Health Summary ==="
  echo "Passed: $passed/$total_checks"
  [ "$warned" -gt 0 ] && echo "Warnings: $warned"
  [ "$failed" -gt 0 ] && echo "Failed: $failed"
  echo ""
fi

# --- Alert if requested ---
if [ "$failed" -gt 0 ]; then
  if [ "$ALERT" = "--alert" ] || [ "$VERBOSE" = "--alert" ]; then
    alert_text=$(printf '🚨 Kronos Agent OS Health Alert\n\nFailed checks (%d/%d):\n' "$failed" "$total_checks")
    for err in "${errors[@]}"; do
      alert_text+="- $err"$'\n'
    done

    # Send to Telegram
    if [ -n "$WEBHOOK_SECRET" ]; then
      json=$(python3 -c "import json,sys; print(json.dumps({'text': sys.argv[1]}))" "$alert_text" 2>/dev/null)
      curl -s -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
        -d "$json" > /dev/null 2>&1
    fi

    # Send to NTFY (phone push notification)
    if [ -n "$NTFY_TOKEN" ]; then
      curl -s -d "$alert_text" \
        -H "Title: Kronos Agent OS Health Alert" \
        -H "Priority: urgent" \
        -H "Tags: rotating_light,skull" \
        -H "Authorization: Bearer $NTFY_TOKEN" \
        "$NTFY_URL/$NTFY_TOPIC" > /dev/null 2>&1
    fi
  fi

  # Print errors for non-verbose mode too
  if [ "$VERBOSE" != "--verbose" ] && [ "$VERBOSE" != "-v" ]; then
    for err in "${errors[@]}"; do
      echo "FAIL: $err"
    done
  fi

  exit 1
fi

if [ "$VERBOSE" = "--verbose" ] || [ "$VERBOSE" = "-v" ]; then
  echo "All checks passed."
fi

exit 0
