#!/bin/bash
# notion-helper.sh — Simple Notion CLI wrapper for agent use
# Usage: notion-helper [-w workspace] <command> [args]
#
# Workspaces:
#   personal   — Roman's personal Notion (default)
#   team — Team workspace
#
# Tokens are loaded from env vars: NOTION_TOKEN_PERSONAL, NOTION_TOKEN_JOURNEYBAY
# Fallback: load from /opt/kronos-ii/app/.env if the file exists

# Load .env if tokens not already in environment
if [ -z "${NOTION_TOKEN_PERSONAL:-}" ] || [ -z "${NOTION_TOKEN_JOURNEYBAY:-}" ]; then
  if [ -f /opt/kronos-ii/app/.env ]; then
    # shellcheck disable=SC1091
    source /opt/kronos-ii/app/.env 2>/dev/null || true
  fi
fi

# Workspace tokens (from env vars)
declare -A WORKSPACES
WORKSPACES[personal]="${NOTION_TOKEN_PERSONAL:-}"
WORKSPACES[team]="${NOTION_TOKEN_TEAM:-}"

# Parse -w flag
WORKSPACE="personal"
if [ "$1" = "-w" ] || [ "$1" = "--workspace" ]; then
  WORKSPACE="$2"
  shift 2
fi

NOTION_KEY="${WORKSPACES[$WORKSPACE]:-}"
if [ -z "$NOTION_KEY" ]; then
  if [ -z "${WORKSPACES[$WORKSPACE]+x}" ]; then
    echo "ERROR: Unknown workspace '$WORKSPACE'. Available: ${!WORKSPACES[*]}"
  else
    echo "ERROR: Token for workspace '$WORKSPACE' is not set. Set NOTION_TOKEN_${WORKSPACE^^} env var or add it to /opt/kronos-ii/app/.env"
  fi
  exit 1
fi

NOTION_VERSION="2025-09-03"
BASE="https://api.notion.com/v1"

api() {
  local method="$1" endpoint="$2" data="$3"
  if [ -n "$data" ]; then
    curl -s -X "$method" "$BASE$endpoint" \
      -H "Authorization: Bearer $NOTION_KEY" \
      -H "Notion-Version: $NOTION_VERSION" \
      -H "Content-Type: application/json" \
      -d "$data"
  else
    curl -s -X "$method" "$BASE$endpoint" \
      -H "Authorization: Bearer $NOTION_KEY" \
      -H "Notion-Version: $NOTION_VERSION"
  fi
}

case "$1" in
  search)
    api POST "/search" "{\"query\": \"$2\"}"
    ;;

  tasks-by-date)
    DB_ID="$2"
    DATE="$3"
    DATE_PROP="${4:-Date}"
    api POST "/data_sources/$DB_ID/query" "{
      \"filter\": {
        \"property\": \"$DATE_PROP\",
        \"date\": {\"equals\": \"$DATE\"}
      }
    }"
    ;;

  tasks-range)
    DB_ID="$2"
    START="$3"
    END="$4"
    DATE_PROP="${5:-Date}"
    api POST "/data_sources/$DB_ID/query" "{
      \"filter\": {
        \"and\": [
          {\"property\": \"$DATE_PROP\", \"date\": {\"on_or_after\": \"$START\"}},
          {\"property\": \"$DATE_PROP\", \"date\": {\"on_or_before\": \"$END\"}}
        ]
      }
    }"
    ;;

  tasks-active)
    DB_ID="$2"
    api POST "/data_sources/$DB_ID/query" "{
      \"filter\": {
        \"and\": [
          {\"property\": \"Status\", \"status\": {\"does_not_equal\": \"Done\"}},
          {\"property\": \"Status\", \"status\": {\"does_not_equal\": \"Completed\"}}
        ]
      },
      \"sorts\": [{\"property\": \"Due Date\", \"direction\": \"ascending\"}]
    }"
    ;;

  db-schema)
    api GET "/data_sources/$2"
    ;;

  page)
    api GET "/pages/$2"
    ;;

  blocks)
    api GET "/blocks/$2/children"
    ;;

  db-query)
    DB_ID="$2"
    if [ -n "$3" ]; then
      api POST "/data_sources/$DB_ID/query" "$3"
    else
      api POST "/data_sources/$DB_ID/query" "{}"
    fi
    ;;

  databases)
    api POST "/search" '{"filter":{"property":"object","value":"data_source"}}'
    ;;

  workspaces)
    echo "Available workspaces:"
    for ws in "${!WORKSPACES[@]}"; do
      token_status="(token set)"
      [ -z "${WORKSPACES[$ws]}" ] && token_status="(token NOT set)"
      if [ "$ws" = "$WORKSPACE" ]; then
        echo "  * $ws (active) $token_status"
      else
        echo "    $ws $token_status"
      fi
    done
    ;;

  *)
    echo "Usage: notion-helper [-w workspace] <command> [args]"
    echo ""
    echo "Workspaces: ${!WORKSPACES[*]} (default: personal)"
    echo "  -w personal     Roman's personal Notion"
    echo "  -w team       Team workspace"
    echo ""
    echo "Tokens: set NOTION_TOKEN_PERSONAL and NOTION_TOKEN_JOURNEYBAY in env"
    echo "  or add them to /opt/kronos-ii/app/.env"
    echo ""
    echo "Commands:"
    echo "  search <query>                    Search pages and databases"
    echo "  databases                         List all databases"
    echo "  db-schema <db_id>                 Show database schema"
    echo "  db-query <db_id> [json_filter]    Raw database query"
    echo "  tasks-by-date <db_id> <date>      Tasks on specific date"
    echo "  tasks-range <db_id> <from> <to>   Tasks in date range"
    echo "  tasks-active <db_id>              Active (not Done) tasks"
    echo "  page <page_id>                    Get page details"
    echo "  blocks <page_id>                  Get page content"
    echo "  workspaces                        List available workspaces"
    ;;
esac
