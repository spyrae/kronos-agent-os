#!/bin/bash
# security-audit.sh — Kronos Agent OS Security Audit Report
# Usage: security-audit.sh [today|week|all]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_log_resolver.sh
source "$SCRIPT_DIR/_log_resolver.sh"
kaos_init_env
APP_DIR="$KAOS_APP_DIR"
MAIN_UNIT="$KAOS_MAIN_UNIT_RESOLVED"
kaos_resolve_log_sources
SECURITY_ARGS=()
AUDIT_ARGS=()
for i in "${!KAOS_LOG_DIRS[@]}"; do
  security_path="${KAOS_LOG_DIRS[$i]}/security.jsonl"
  audit_path="${KAOS_LOG_DIRS[$i]}/audit.jsonl"
  if [ -f "$security_path" ]; then
    SECURITY_ARGS+=("${KAOS_LOG_LABELS[$i]}=$security_path")
  fi
  if [ -f "$audit_path" ]; then
    AUDIT_ARGS+=("${KAOS_LOG_LABELS[$i]}=$audit_path")
  fi
done
WORKSPACE_AUDIT_PATH="$KAOS_WORKSPACE_PATH_RESOLVED"

PERIOD="${1:-today}"
TODAY=$(date -u +%Y-%m-%d)
WEEK_AGO=$(date -u -d "7 days ago" +%Y-%m-%d 2>/dev/null || date -u -v-7d +%Y-%m-%d)

case "$PERIOD" in
  today) FILTER_START="$TODAY"; LABEL="Today ($TODAY)" ;;
  week)  FILTER_START="$WEEK_AGO"; LABEL="Last 7 days (since $WEEK_AGO)" ;;
  all)   FILTER_START=""; LABEL="All time" ;;
  *)     echo "Usage: security-audit.sh [today|week|all]"; exit 1 ;;
esac

echo "═══════════════════════════════════════════════"
echo "  Kronos Agent OS Security Audit: $LABEL"
echo "═══════════════════════════════════════════════"
echo ""
echo "Log sources:"
if [ "${#KAOS_LOG_DIRS[@]}" -gt 0 ]; then
  for i in "${!KAOS_LOG_DIRS[@]}"; do
    echo "  ${KAOS_LOG_LABELS[$i]} (${KAOS_LOG_REASONS[$i]}): ${KAOS_LOG_DIRS[$i]}"
  done
else
  echo "  none"
fi
for warning in "${KAOS_LOG_WARNINGS[@]}"; do
  echo "  warning: $warning"
done
echo ""

# --- Security events ---
echo "▸ Security Events"
if [ "${#SECURITY_ARGS[@]}" -gt 0 ]; then
  python3 - "$PERIOD" "$FILTER_START" "${SECURITY_ARGS[@]}" <<'PY'
import json
import sys
from collections import defaultdict

period = sys.argv[1]
filter_start = sys.argv[2]
events = defaultdict(int)
recent = []
for source in sys.argv[3:]:
    label, path = source.split("=", 1)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = str(d.get("ts", ""))
            if period == "today" and not ts.startswith(filter_start):
                continue
            if period == "week" and ts[:10] < filter_start:
                continue
            d["_source"] = label
            events[d.get("event", "unknown")] += 1
            recent.append(d)

if not events:
    print("  No security events recorded for this period.")
else:
    for event, count in sorted(events.items(), key=lambda x: -x[1]):
        print(f"  {event}: {count}")
    print()
    print("  Last 5 events:")
    for d in recent[-5:]:
        ts = str(d.get("ts", "?"))[:19]
        event = d.get("event", "?")
        preview = str(d.get("message_preview") or d.get("messagePreview") or "")[:60]
        source = d.get("_source", "?")
        print(f"    [{ts}] {source}/{event}: {preview}")
PY
else
  echo "  security.jsonl not implemented/configured in resolved log sources."
fi

echo ""

# --- Audit summary ---
echo "▸ Audit Summary"
if [ "${#AUDIT_ARGS[@]}" -gt 0 ]; then
  python3 - "$PERIOD" "$FILTER_START" "${AUDIT_ARGS[@]}" <<'PY'
import json
import sys
from collections import defaultdict

period = sys.argv[1]
filter_start = sys.argv[2]
total = 0
blocked = 0
tiers = defaultdict(int)
total_cost = 0.0
total_duration = 0

for source in sys.argv[3:]:
    _, path = source.split("=", 1)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = str(d.get("ts", ""))
            if period == "today" and not ts.startswith(filter_start):
                continue
            if period == "week" and ts[:10] < filter_start:
                continue
            total += 1
            if d.get("blocked"):
                blocked += 1
            tiers[d.get("tier", "unknown")] += 1
            total_cost += float(d.get("approx_cost_usd", 0) or 0)
            total_duration += int(d.get("duration_ms", 0) or 0)

print(f"  Total requests: {total}")
print(f"  Blocked: {blocked} ({blocked / total * 100:.1f}%)" if total else "  Blocked: 0")
print(f"  Total cost: ${total_cost:.4f}")
print(f"  Avg response time: {total_duration / total / 1000:.1f}s" if total else "  Avg response time: N/A")
print()
print("  By tier:")
for tier in sorted(tiers):
    pct = tiers[tier] / total * 100 if total else 0
    print(f"    {tier:10s}: {tiers[tier]:4d} ({pct:.1f}%)")
PY
else
  echo "  audit.jsonl missing / not configured in resolved log sources."
fi

echo ""
echo "▸ System Checks"

# Check services
echo -n "  $MAIN_UNIT:         "
systemctl is-active "$MAIN_UNIT" 2>/dev/null || echo "UNKNOWN"
echo -n "  health timer:   "
systemctl is-active kronos-health.timer 2>/dev/null || echo "UNKNOWN"

# Check API keys exposed in workspace
echo ""
echo -n "  Exposed secrets in workspace: "
if [ ! -d "$WORKSPACE_AUDIT_PATH" ]; then
  echo "not checked (missing directory: $WORKSPACE_AUDIT_PATH)"
else
  EXPOSED=$(grep -rl 'sk-ant\|sk-proj\|ntn_\|AIzaSy' "$WORKSPACE_AUDIT_PATH" 2>/dev/null | wc -l | tr -d '[:space:]')
  if [ "$EXPOSED" -gt 0 ]; then
    echo "WARNING: $EXPOSED files contain API keys!"
  else
    echo "OK (none found)"
  fi
fi

# Check log sizes
echo ""
echo "▸ Log Sizes"
if [ "${#SECURITY_ARGS[@]}" -eq 0 ]; then
  echo "  security.jsonl: not implemented/configured (no writer found in codebase)"
fi
if [ "${#AUDIT_ARGS[@]}" -eq 0 ]; then
  echo "  audit.jsonl: missing / not configured"
fi
for arg in "${SECURITY_ARGS[@]}" "${AUDIT_ARGS[@]}"; do
  logfile="${arg#*=}"
  if [ -n "$logfile" ] && [ -f "$logfile" ]; then
    SIZE=$(du -h "$logfile" | cut -f1)
    LINES=$(wc -l < "$logfile")
    echo "  $(basename "$logfile"): $SIZE ($LINES entries)"
  fi
done
router_found=0
for dir in "${KAOS_LOG_DIRS[@]}"; do
  logfile="$dir/router-cost.jsonl"
  if [ -f "$logfile" ]; then
    router_found=1
    SIZE=$(du -h "$logfile" | cut -f1)
    LINES=$(wc -l < "$logfile")
    echo "  $(basename "$logfile"): $SIZE ($LINES entries)"
  fi
done
if [ "$router_found" -eq 0 ]; then
  echo "  router-cost.jsonl: not implemented/configured (no writer found in codebase)"
fi

echo ""
echo "═══════════════════════════════════════════════"
