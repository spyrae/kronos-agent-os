#!/bin/bash
# daily-status.sh — Daily status report for Kronos Agent OS stack
# Sends summary to NTFY with uptime, service status, and log stats
#
# Usage: daily-status.sh
# Designed to run via cron at 23:00 UTC daily

set -uo pipefail

# Resolve the install dir relative to this script (works on any deploy path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_log_resolver.sh
source "$SCRIPT_DIR/_log_resolver.sh"
kaos_init_env
APP_DIR="$KAOS_APP_DIR"

# Main agent systemd unit name. Generic default; override via KAOS_MAIN_UNIT.
MAIN_UNIT="$KAOS_MAIN_UNIT_RESOLVED"
HEALTH_UNIT="$KAOS_HEALTH_UNIT_RESOLVED"

kaos_resolve_log_sources
AUDIT_ARGS=()
for i in "${!KAOS_LOG_DIRS[@]}"; do
  audit_path="${KAOS_LOG_DIRS[$i]}/audit.jsonl"
  if [ -f "$audit_path" ]; then
    AUDIT_ARGS+=("${KAOS_LOG_LABELS[$i]}=$audit_path")
  fi
done

# --- Gather data ---

# Service status
service_status=$(systemctl is-active "$MAIN_UNIT" 2>/dev/null || echo "unknown")

# Service uptime (from systemd ActiveEnterTimestamp)
service_started=$(systemctl show "$MAIN_UNIT" --property=ActiveEnterTimestamp --value 2>/dev/null || echo "")
if [ -n "$service_started" ] && [ "$service_started" != "n/a" ]; then
  started_ts=$(date -d "$service_started" +%s 2>/dev/null) || started_ts=0
  now_ts=$(date +%s)
  if [ "$started_ts" -gt 0 ] 2>/dev/null; then
    uptime_hours=$(( (now_ts - started_ts) / 3600 ))
    uptime_days=$(( uptime_hours / 24 ))
    uptime_remainder=$(( uptime_hours % 24 ))
    uptime_str="${uptime_days}d ${uptime_remainder}h"
  else
    uptime_str="N/A"
  fi
else
  uptime_str="N/A"
fi

# Bridge health
bridge_health="unreachable"
bridge_response=$(curl -sf --max-time 5 "http://127.0.0.1:8788/health" 2>/dev/null)
if echo "$bridge_response" | grep -q '"status".*"ok"'; then
  bridge_health="ok"
fi

# Dashboard health
dashboard_health="unreachable"
dashboard_response=$(curl -sf --max-time 5 "http://127.0.0.1:8789/api/health" 2>/dev/null)
if [ -n "$dashboard_response" ]; then
  dashboard_health="ok"
fi

# System uptime
sys_uptime=$(uptime -p 2>/dev/null | sed 's/^up //' || echo "N/A")

# Disk usage
disk_usage=$(df -h / 2>/dev/null | awk 'NR==2 {print $5 " (" $3 "/" $2 ")"}')

# Memory usage
mem_info=$(free -h 2>/dev/null | awk 'NR==2 {print $3 "/" $2}')

# Health check results from systemd journal.
health_journal=$(journalctl -u "$HEALTH_UNIT" --since "24 hours ago" --no-pager 2>/dev/null || true)
if [ -n "$health_journal" ]; then
  health_runs=$(printf '%s\n' "$health_journal" | grep -Eci 'Started|Finished|Health Summary|All checks passed|FAIL:')
  health_fails=$(printf '%s\n' "$health_journal" | grep -Eci 'FAIL:|Failed|Health Alert|not responding|critical')
  if [ "$health_runs" -eq 0 ] 2>/dev/null; then
    health_runs=$(printf '%s\n' "$health_journal" | wc -l | tr -d ' ')
  fi
else
  if command -v journalctl >/dev/null 2>&1; then
    health_runs="no journal entries"
  else
    health_runs="journal unavailable"
  fi
  health_fails="N/A"
fi

# Recent service restarts (last 24h)
service_restarts=$(journalctl -u "$MAIN_UNIT" --since "24 hours ago" 2>/dev/null | grep -c "Started\|Stopped" 2>/dev/null) || service_restarts=0

audit_stats="missing / not configured"
audit_source="none"
if [ "${#AUDIT_ARGS[@]}" -gt 0 ]; then
  audit_today=$(python3 - "$(date -u +%Y-%m-%d)" "${AUDIT_ARGS[@]}" <<'PY'
import json
import sys

today = sys.argv[1]
count = 0
for source in sys.argv[2:]:
    _, path = source.split("=", 1)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if str(entry.get("ts", "")).startswith(today):
                count += 1
print(count)
PY
)
  audit_stats="${audit_today} requests today"
  audit_source="${KAOS_LOG_MODE_RESOLVED}: ${#AUDIT_ARGS[@]} source(s)"
elif [ "${#KAOS_LOG_DIRS[@]}" -gt 0 ]; then
  audit_stats="missing at ${KAOS_LOG_DIRS[0]}/audit.jsonl"
  audit_source="${KAOS_LOG_LABELS[0]} (${KAOS_LOG_REASONS[0]})"
fi

# --- Build report ---

status_icon="white_check_mark"
status_word="OK"

if [ "$service_status" != "active" ] || [ "$bridge_health" != "ok" ]; then
  status_icon="warning"
  status_word="DEGRADED"
fi

report=$(cat <<EOF
Kronos Agent OS Daily Status [$status_word]

Services:
  $MAIN_UNIT: $service_status
  Bridge (8788): $bridge_health
  Dashboard (8789): $dashboard_health

Uptime:
  Service: $uptime_str
  System: $sys_uptime

Resources:
  Disk: $disk_usage
  Memory: $mem_info

Health checks (24h):
  Unit: $HEALTH_UNIT
  Runs: $health_runs
  Failures: $health_fails

Service events (24h):
  Start/stop: $service_restarts

Audit:
  $audit_stats
  Source: $audit_source
EOF
)

# --- Send to NTFY ---

if [ -n "$NTFY_TOKEN" ]; then
  curl -s -d "$report" \
    -H "Title: Kronos Agent OS Daily Status" \
    -H "Priority: low" \
    -H "Tags: $status_icon,robot_face" \
    -H "Authorization: Bearer $NTFY_TOKEN" \
    "$NTFY_URL/$NTFY_TOPIC" > /dev/null 2>&1
  echo "Daily status sent to NTFY"
else
  echo "NTFY_TOKEN not set, printing report:"
  echo "$report"
fi

exit 0
