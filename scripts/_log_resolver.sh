#!/bin/bash
# Shared log-path resolver for KAOS ops scripts.
#
# Contract mirrors kronos.ops.logs:
#   KAOS_LOG_DIR -> explicit single log dir
#   KAOS_LOG_MODE=aggregate or *_AGENT_NAME=all -> data/*/logs
#   DB_PATH -> dirname(DB_PATH)/logs
#   DB_DIR -> DB_DIR/logs
#   otherwise -> data/${KAOS_AGENT_NAME:-${AGENT_NAME:-kronos}}/logs

# shellcheck source=scripts/_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

kaos_init_env() {
  if [ -n "${KAOS_COMMON_INITIALIZED:-}" ]; then
    return 0
  fi

  kaos_common_init
}

kaos_resolve_log_sources() {
  kaos_init_env

  KAOS_LOG_MODE_RESOLVED="single"
  KAOS_LOG_DIRS=()
  KAOS_LOG_LABELS=()
  KAOS_LOG_REASONS=()
  KAOS_LOG_WARNINGS=()

  if [ -n "${KAOS_LOG_DIR:-}" ]; then
    KAOS_LOG_DIRS=("$(kaos_abs_path "$KAOS_LOG_DIR")")
    KAOS_LOG_LABELS=("explicit")
    KAOS_LOG_REASONS=("KAOS_LOG_DIR")
    return 0
  fi

  local agent_name="$KAOS_AGENT_NAME_RESOLVED"
  local mode="${KAOS_LOG_MODE:-}"
  if [ "$mode" = "aggregate" ] || [ "$mode" = "all" ] || [ "$agent_name" = "all" ]; then
    KAOS_LOG_MODE_RESOLVED="aggregate"
    local found=0
    local dir
    shopt -s nullglob
    for dir in "$KAOS_APP_DIR"/data/*/logs; do
      if [ -d "$dir" ]; then
        KAOS_LOG_DIRS+=("$dir")
        KAOS_LOG_LABELS+=("$(basename "$(dirname "$dir")")")
        KAOS_LOG_REASONS+=("aggregate:data/*/logs")
        found=1
      fi
    done
    shopt -u nullglob
    if [ "$found" -eq 0 ]; then
      KAOS_LOG_WARNINGS+=("no log directories found under $KAOS_APP_DIR/data")
    fi
    return 0
  fi

  if [ -n "${DB_PATH:-}" ]; then
    local db_path db_dir label
    db_path="$(kaos_abs_path "$DB_PATH")"
    db_dir="$(dirname "$db_path")"
    label="$(basename "$db_dir")"
    KAOS_LOG_DIRS=("$db_dir/logs")
    KAOS_LOG_LABELS=("${label:-db-path}")
    KAOS_LOG_REASONS=("DB_PATH")
    return 0
  fi

  if [ -n "${DB_DIR:-}" ]; then
    local resolved_db_dir
    resolved_db_dir="$(kaos_abs_path "$DB_DIR")"
    KAOS_LOG_DIRS=("$resolved_db_dir/logs")
    KAOS_LOG_LABELS=("$(basename "$resolved_db_dir")")
    KAOS_LOG_REASONS=("DB_DIR")
    return 0
  fi

  KAOS_LOG_DIRS=("$KAOS_APP_DIR/data/$agent_name/logs")
  KAOS_LOG_LABELS=("$agent_name")
  KAOS_LOG_REASONS=("AGENT_NAME")
}

kaos_log_jsonl_paths() {
  local filename="$1"
  kaos_resolve_log_sources
  local dir
  for dir in "${KAOS_LOG_DIRS[@]}"; do
    printf '%s/%s\n' "$dir" "$filename"
  done
}

kaos_first_existing_log_file() {
  local filename="$1"
  kaos_resolve_log_sources
  local dir path
  for dir in "${KAOS_LOG_DIRS[@]}"; do
    path="$dir/$filename"
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}
