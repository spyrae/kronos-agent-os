#!/bin/bash
# cost-stats.sh — Show LLM cost statistics from audit log
# Usage: cost-stats.sh [today|week|all]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_log_resolver.sh
source "$SCRIPT_DIR/_log_resolver.sh"
kaos_resolve_log_sources
COST_ARGS=()
AUDIT_ARGS=()
for i in "${!KAOS_LOG_DIRS[@]}"; do
  cost_path="${KAOS_LOG_DIRS[$i]}/cost.jsonl"
  audit_path="${KAOS_LOG_DIRS[$i]}/audit.jsonl"
  if [ -f "$cost_path" ]; then
    COST_ARGS+=("${KAOS_LOG_LABELS[$i]}=$cost_path")
  fi
  if [ -f "$audit_path" ]; then
    AUDIT_ARGS+=("${KAOS_LOG_LABELS[$i]}=$audit_path")
  fi
done

if [ "${#COST_ARGS[@]}" -eq 0 ]; then
  echo "No cost log found for resolved sources:"
  for i in "${!KAOS_LOG_DIRS[@]}"; do
    echo "  ${KAOS_LOG_LABELS[$i]} (${KAOS_LOG_REASONS[$i]}): ${KAOS_LOG_DIRS[$i]}/cost.jsonl"
  done
  for warning in "${KAOS_LOG_WARNINGS[@]}"; do
    echo "  warning: $warning"
  done
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

echo "=== Kronos Agent OS Cost Stats: $LABEL ==="
echo "Sources:"
for arg in "${COST_ARGS[@]}"; do
  echo "  ${arg%%=*}: ${arg#*=}"
done
echo ""

python3 - "$PERIOD" "$FILTER_PREFIX" "${COST_ARGS[@]}" <<'PY'
import json
import sys
from collections import defaultdict

period = sys.argv[1]
filter_prefix = sys.argv[2]
sources = sys.argv[3:]

stats = defaultdict(lambda: {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0})
source_stats = defaultdict(lambda: {"count": 0, "cost": 0.0})
total_cost = 0.0
total_count = 0

for source in sources:
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

            ts = d.get("ts", "")
            if period == "today" and not ts.startswith(filter_prefix):
                continue
            if period == "week" and ts[:10] < filter_prefix:
                continue

            tier = d.get("tier", "unknown")
            cost = float(d.get("cost_usd", 0) or 0)
            stats[tier]["count"] += 1
            stats[tier]["cost"] += cost
            stats[tier]["input_tokens"] += int(d.get("input_tokens", 0) or 0)
            stats[tier]["output_tokens"] += int(d.get("output_tokens", 0) or 0)
            source_stats[label]["count"] += 1
            source_stats[label]["cost"] += cost
            total_cost += cost
            total_count += 1

if total_count == 0:
    print("  No requests found for this period.")
    sys.exit(0)

if len(source_stats) > 1:
    print("Requests by source:")
    for label in sorted(source_stats):
        s = source_stats[label]
        print(f"  {label:10s}: {s['count']:4d} requests  ${s['cost']:.4f}")
    print()

print("Requests by tier:")
for tier in sorted(stats):
    s = stats[tier]
    pct = s["count"] / total_count * 100
    avg_tokens = (s["input_tokens"] + s["output_tokens"]) // max(s["count"], 1)
    print(f"  {tier:10s}: {s['count']:4d} requests ({pct:5.1f}%)  ${s['cost']:.4f}  avg {avg_tokens} tok/req")

print()
print(f"  Total: {total_count} requests, ${total_cost:.4f}")

# Savings estimate: if all were Sonnet ($3/1M input + $15/1M output)
all_input = sum(s["input_tokens"] for s in stats.values())
all_output = sum(s["output_tokens"] for s in stats.values())
sonnet_cost = (all_input * 3 + all_output * 15) / 1_000_000
if sonnet_cost > total_cost:
    print(f"  Estimated savings vs all-Sonnet: ${sonnet_cost - total_cost:.4f}")
PY

# Blocked requests from audit log
if [ "${#AUDIT_ARGS[@]}" -gt 0 ]; then
  echo ""
  blocked=$(python3 - "$PERIOD" "$FILTER_PREFIX" "${AUDIT_ARGS[@]}" <<'PY'
import json
import sys

period = sys.argv[1]
filter_prefix = sys.argv[2]
count = 0
for source in sys.argv[3:]:
    _, path = source.split("=", 1)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("ts", "")
            if period == "today" and not ts.startswith(filter_prefix):
                continue
            if period == "week" and ts[:10] < filter_prefix:
                continue
            if d.get("blocked"):
                count += 1
print(count)
PY
)
  echo "Blocked requests ($PERIOD): $blocked"
fi
