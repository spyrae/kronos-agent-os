#!/bin/bash
# cost-stats.sh — Show LLM cost statistics from audit log
# Usage: cost-stats.sh [today|week|all]

COST_LOG="/opt/kronos-ii/data/logs/cost.jsonl"
AUDIT_LOG="/opt/kronos-ii/data/logs/audit.jsonl"

if [ ! -f "$COST_LOG" ]; then
  echo "No cost log found at $COST_LOG"
  exit 0
fi

PERIOD="${1:-today}"
TODAY=$(date -u +%Y-%m-%d)

case "$PERIOD" in
  today)
    FILTER_PREFIX="$TODAY"
    LABEL="Today ($TODAY)"
    ;;
  week)
    WEEK_AGO=$(date -u -d "7 days ago" +%Y-%m-%d 2>/dev/null || date -u -v-7d +%Y-%m-%d)
    FILTER_PREFIX="$WEEK_AGO"
    LABEL="Last 7 days (since $WEEK_AGO)"
    ;;
  all)
    FILTER_PREFIX=""
    LABEL="All time"
    ;;
  *)
    echo "Usage: cost-stats.sh [today|week|all]"
    exit 1
    ;;
esac

echo "=== Kronos II Cost Stats: $LABEL ==="
echo ""

python3 -c "
import json, sys
from collections import defaultdict

stats = defaultdict(lambda: {'count': 0, 'cost': 0.0, 'input_tokens': 0, 'output_tokens': 0})
total_cost = 0.0
total_count = 0
filter_prefix = '$FILTER_PREFIX'
period = '$PERIOD'

for line in open('$COST_LOG'):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except:
        continue

    ts = d.get('ts', '')
    if period == 'today' and not ts.startswith(filter_prefix):
        continue
    if period == 'week' and ts < filter_prefix:
        continue

    tier = d.get('tier', 'unknown')
    cost = d.get('cost_usd', 0)
    stats[tier]['count'] += 1
    stats[tier]['cost'] += cost
    stats[tier]['input_tokens'] += d.get('input_tokens', 0)
    stats[tier]['output_tokens'] += d.get('output_tokens', 0)
    total_cost += cost
    total_count += 1

if total_count == 0:
    print('  No requests found for this period.')
    sys.exit(0)

print('Requests by tier:')
for tier in sorted(stats):
    s = stats[tier]
    pct = (s['count'] / total_count * 100)
    avg_tokens = (s['input_tokens'] + s['output_tokens']) // max(s['count'], 1)
    print(f'  {tier:10s}: {s[\"count\"]:4d} requests ({pct:5.1f}%)  \${s[\"cost\"]:.4f}  avg {avg_tokens} tok/req')

print()
print(f'  Total: {total_count} requests, \${total_cost:.4f}')

# Savings estimate: if all were Sonnet (\$3/1M input + \$15/1M output)
all_input = sum(s['input_tokens'] for s in stats.values())
all_output = sum(s['output_tokens'] for s in stats.values())
sonnet_cost = (all_input * 3 + all_output * 15) / 1_000_000
if sonnet_cost > total_cost:
    print(f'  Estimated savings vs all-Sonnet: \${sonnet_cost - total_cost:.4f}')
"

# Blocked requests from audit log
if [ -f "$AUDIT_LOG" ]; then
  echo ""
  blocked=$(python3 -c "
import json
count = 0
for line in open('$AUDIT_LOG'):
    try:
        d = json.loads(line)
        if d.get('blocked'):
            count += 1
    except:
        pass
print(count)
")
  echo "Blocked requests (all time): $blocked"
fi
