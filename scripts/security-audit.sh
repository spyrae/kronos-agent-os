#!/bin/bash
# security-audit.sh — Kronos II Security Audit Report
# Usage: security-audit.sh [today|week|all]

SECURITY_LOG="/opt/kronos-ii/data/security.jsonl"
AUDIT_LOG="/opt/kronos-ii/data/audit.jsonl"
WORKSPACE_PATH="/opt/kronos-ii/workspace"

PERIOD="${1:-today}"
TODAY=$(date -u +%Y-%m-%d)

case "$PERIOD" in
  today) LABEL="Today ($TODAY)" ;;
  week)  LABEL="Last 7 days" ;;
  all)   LABEL="All time" ;;
  *)     echo "Usage: security-audit.sh [today|week|all]"; exit 1 ;;
esac

echo "═══════════════════════════════════════════════"
echo "  Kronos II Security Audit: $LABEL"
echo "═══════════════════════════════════════════════"
echo ""

# --- Security events ---
echo "▸ Security Events"
if [ -f "$SECURITY_LOG" ]; then
  python3 -c "
import json, sys
from collections import defaultdict

events = defaultdict(int)
recent = []
for line in open('$SECURITY_LOG'):
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
    except: continue
    events[d.get('event', 'unknown')] += 1
    recent.append(d)

if not events:
    print('  No security events recorded.')
else:
    for event, count in sorted(events.items(), key=lambda x: -x[1]):
        print(f'  {event}: {count}')
    print()
    print('  Last 5 events:')
    for d in recent[-5:]:
        ts = d.get('ts', '?')[:19]
        event = d.get('event', '?')
        preview = d.get('messagePreview', '')[:60]
        print(f'    [{ts}] {event}: {preview}')
"
else
  echo "  No security log found."
fi

echo ""

# --- Audit summary ---
echo "▸ Audit Summary"
if [ -f "$AUDIT_LOG" ]; then
  python3 -c "
import json, sys
from collections import defaultdict

total = 0
blocked = 0
tiers = defaultdict(int)
total_cost = 0.0
total_duration = 0

for line in open('$AUDIT_LOG'):
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
    except: continue
    total += 1
    if d.get('blocked'):
        blocked += 1
    tiers[d.get('tier', 'unknown')] += 1
    total_cost += d.get('approxCost', 0)
    total_duration += d.get('durationMs', 0)

print(f'  Total requests: {total}')
print(f'  Blocked: {blocked} ({blocked/total*100:.1f}%)' if total else '  Blocked: 0')
print(f'  Total cost: \${total_cost:.4f}')
print(f'  Avg response time: {total_duration/total/1000:.1f}s' if total else '  Avg response time: N/A')
print()
print('  By tier:')
for tier in sorted(tiers):
    pct = tiers[tier] / total * 100 if total else 0
    print(f'    {tier:10s}: {tiers[tier]:4d} ({pct:.1f}%)')
"
else
  echo "  No audit log found."
fi

echo ""
echo "▸ System Checks"

# Check services
echo -n "  kronos-ii:         "
systemctl is-active kronos-ii 2>/dev/null || echo "UNKNOWN"
echo -n "  heartbeat timer:   "
systemctl is-active kronos-heartbeat.timer 2>/dev/null || echo "UNKNOWN"

# Check API keys exposed in workspace
echo ""
echo -n "  Exposed secrets in workspace: "
EXPOSED=$(grep -rl 'sk-ant\|sk-proj\|ntn_\|AIzaSy' "$WORKSPACE_PATH" 2>/dev/null | wc -l)
if [ "$EXPOSED" -gt 0 ]; then
  echo "WARNING: $EXPOSED files contain API keys!"
else
  echo "OK (none found)"
fi

# Check log sizes
echo ""
echo "▸ Log Sizes"
for logfile in "$SECURITY_LOG" "$AUDIT_LOG" "/opt/kronos-ii/data/router-cost.jsonl"; do
  if [ -f "$logfile" ]; then
    SIZE=$(du -h "$logfile" | cut -f1)
    LINES=$(wc -l < "$logfile")
    echo "  $(basename $logfile): $SIZE ($LINES entries)"
  fi
done

echo ""
echo "═══════════════════════════════════════════════"
