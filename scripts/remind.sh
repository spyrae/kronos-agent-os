#!/bin/bash
# remind.sh — Schedule a reminder that delivers to Telegram via webhook
# Usage: remind <delay> <message>
# Examples:
#   remind 30m "Check the oven"
#   remind 2h "Call the dentist"
#   remind 1d "Review weekly goals"
#   remind 10:30 "Stand-up meeting"    (at specific time today, or tomorrow if past)

WEBHOOK_URL="${REMINDER_WEBHOOK_URL:-http://127.0.0.1:8788/webhook}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"

if [ -z "$WEBHOOK_SECRET" ]; then
  echo "ERROR: WEBHOOK_SECRET env var is not set"
  exit 1
fi

if [ $# -lt 2 ]; then
  echo "Usage: remind <delay|time> <message>"
  echo "  delay: 30s, 5m, 2h, 1d"
  echo "  time:  10:30, 14:00 (24h format)"
  echo "  message: reminder text"
  exit 1
fi

DELAY="$1"
shift
MESSAGE="$*"

# Parse delay/time into seconds for `sleep`
parse_delay() {
  local input="$1"

  # Time format HH:MM
  if echo "$input" | grep -qE '^[0-9]{1,2}:[0-9]{2}$'; then
    local target_ts=$(date -d "today $input" +%s 2>/dev/null || date -j -f "%H:%M" "$input" +%s 2>/dev/null)
    local now_ts=$(date +%s)
    local diff=$((target_ts - now_ts))
    if [ "$diff" -lt 0 ]; then
      diff=$((diff + 86400))  # tomorrow
    fi
    echo "$diff"
    return
  fi

  # Duration format: Ns, Nm, Nh, Nd
  local num=$(echo "$input" | sed 's/[^0-9]//g')
  local unit=$(echo "$input" | sed 's/[0-9]//g')

  case "$unit" in
    s) echo "$num" ;;
    m) echo $((num * 60)) ;;
    h) echo $((num * 3600)) ;;
    d) echo $((num * 86400)) ;;
    *) echo "0" ;;
  esac
}

SECS=$(parse_delay "$DELAY")

if [ "$SECS" -le 0 ]; then
  echo "ERROR: Invalid delay format: $DELAY"
  exit 1
fi

# Calculate delivery time for display
if command -v date > /dev/null; then
  DELIVER_AT=$(date -d "+${SECS} seconds" "+%H:%M" 2>/dev/null || date -v "+${SECS}S" "+%H:%M" 2>/dev/null || echo "in ${DELAY}")
fi

# Build JSON safely using printf and python/node
build_json() {
  if command -v python3 > /dev/null 2>&1; then
    python3 -c "import json; print(json.dumps({'text': '''$1'''}))"
  elif command -v node > /dev/null 2>&1; then
    node -e "process.stdout.write(JSON.stringify({text: process.argv[1]}))" "$1"
  else
    # Fallback: simple escaping
    local escaped=$(echo "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g')
    echo "{\"text\":\"$escaped\"}"
  fi
}

# Schedule in background
(
  sleep "$SECS"
  JSON_BODY=$(build_json "$MESSAGE")
  curl -s -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
    -d "$JSON_BODY" > /dev/null 2>&1
) &

REMINDER_PID=$!
echo "OK: Reminder scheduled (PID $REMINDER_PID, delivery ~${DELIVER_AT}): $MESSAGE"
