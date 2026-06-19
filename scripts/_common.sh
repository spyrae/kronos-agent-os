#!/bin/bash
# Shared env/path helper for Kronos Agent OS shell scripts.
#
# Usage contract:
#   source "$SCRIPT_DIR/_common.sh"
#   kaos_common_init
#
# Resolution order:
#   1. Resolve KAOS_APP_DIR from the helper location unless process env sets it.
#   2. Safely load KAOS_APP_DIR/.env without executing shell code.
#   3. Process env wins over .env; .env wins over built-in defaults.
#   4. Expose resolved KAOS_* helper variables for scripts and other helpers.

kaos_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

kaos_load_env_file() {
  local env_file="$1"
  [ -f "$env_file" ] || return 0

  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(kaos_trim "$line")"
    case "$line" in
      ""|\#*) continue ;;
      *=*) ;;
      *) continue ;;
    esac

    key="$(kaos_trim "${line%%=*}")"
    value="$(kaos_trim "${line#*=}")"
    case "$key" in
      ""|*[!A-Za-z0-9_]*|[0-9]*)
        continue
        ;;
    esac

    # Process env has priority over .env.
    if [ -n "${!key+x}" ]; then
      continue
    fi

    case "$value" in
      \"*\")
        value="${value#\"}"
        value="${value%\"}"
        ;;
      \'*\')
        value="${value#\'}"
        value="${value%\'}"
        ;;
    esac
    export "$key=$value"
  done < "$env_file"
}

kaos_abs_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    (cd "$(dirname "$path")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$path")") || printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$KAOS_APP_DIR" "$path"
  fi
}

kaos_common_init() {
  if [ -n "${KAOS_COMMON_INITIALIZED:-}" ]; then
    return 0
  fi

  local common_dir
  common_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  KAOS_SCRIPT_DIR="${KAOS_SCRIPT_DIR:-$common_dir}"

  if [ -z "${KAOS_APP_DIR:-}" ]; then
    KAOS_APP_DIR="$(cd "$common_dir/.." && pwd)"
  elif [ -d "$KAOS_APP_DIR" ]; then
    KAOS_APP_DIR="$(cd "$KAOS_APP_DIR" && pwd)"
  fi

  kaos_load_env_file "$KAOS_APP_DIR/.env"

  KAOS_AGENT_NAME_RESOLVED="${KAOS_AGENT_NAME:-${AGENT_NAME:-kronos}}"
  KAOS_MAIN_UNIT_RESOLVED="${KAOS_MAIN_UNIT:-kaos}"
  KAOS_HEALTH_UNIT_RESOLVED="${KAOS_HEALTH_UNIT:-kronos-health.service}"

  NTFY_URL="${NTFY_URL:-https://ntfy.sh}"
  NTFY_TOKEN="${NTFY_TOKEN:-}"
  NTFY_TOPIC="${NTFY_TOPIC:-persona-alerts}"

  KAOS_WORKSPACES_DIR_RESOLVED="$KAOS_APP_DIR/workspaces"
  if [ -n "${WORKSPACE_PATH:-}" ]; then
    KAOS_WORKSPACE_PATH_RESOLVED="$(kaos_abs_path "$WORKSPACE_PATH")"
  else
    KAOS_WORKSPACE_PATH_RESOLVED="$KAOS_WORKSPACES_DIR_RESOLVED/$KAOS_AGENT_NAME_RESOLVED"
  fi

  KAOS_COMMON_INITIALIZED=1
  KAOS_ENV_INITIALIZED=1
}
