#!/bin/bash
# daily-status.sh — Daily status report for Kronos Agent OS stack
# Sends summary to NTFY with uptime, service status, and log stats
#
# Usage: daily-status.sh
# Designed to run via cron at 23:00 UTC daily

set -uo pipefail

# NTFY config (loaded from .env if available)
if [ -f /opt/kaos/app/.env ]; then
  # shellcheck disable=SC1091
  source /opt/kaos/app/.env 2>/dev/null || true
fi
NTFY_URL="${NTFY_URL:-${NTFY_URL:-https://ntfy.sh}}"
NTFY_TOKEN="${NTFY_TOKEN:-}"
NTFY_TOPIC="${NTFY_TOPIC:-persona-alerts}"

# --- Gather data ---

# Service status
service_status=$(systemctl is-active kaos 2>/dev/null || echo "unknown")

# Service uptime (from systemd ActiveEnterTimestamp)
service_started=$(systemctl show kaos --property=ActiveEnterTimestamp --value 2>/dev/null || echo "")
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

# Today's health check results (from cron log)
health_log="/var/log/kronos-health.log"
today=$(date '+%Y-%m-%d')
if [ -f "$health_log" ]; then
  health_runs=$(grep -c "$today" "$health_log" 2>/dev/null) || health_runs=0
  health_fails=$(grep "$today" "$health_log" 2>/dev/null | grep -c "FAIL" 2>/dev/null) || health_fails=0
else
  health_runs="no log"
  health_fails="N/A"
fi

# Recent service restarts (last 24h)
service_restarts=$(journalctl -u kaos --since "24 hours ago" 2>/dev/null | grep -c "Started\|Stopped" 2>/dev/null) || service_restarts=0

# Audit log stats (if available)
audit_log="/opt/kaos/data/audit.jsonl"
audit_stats="N/A"
if [ -f "$audit_log" ]; then
  audit_today=$(grep "$(date -u +%Y-%m-%d)" "$audit_log" 2>/dev/null | wc -l | tr -d ' ')
  audit_stats="${audit_today} requests today"
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
  kaos: $service_status
  Bridge (8788): $bridge_health
  Dashboard (8789): $dashboard_health

Uptime:
  Service: $uptime_str
  System: $sys_uptime

Resources:
  Disk: $disk_usage
  Memory: $mem_info

Health checks (24h):
  Runs: $health_runs
  Failures: $health_fails

Service events (24h):
  Start/stop: $service_restarts

Audit:
  $audit_stats
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
