#!/bin/bash
# workspace-backup.sh — Auto-backup workspace files to a private Git repo.
# Refuses to use public app defaults; configure an explicit source/target.
#
# Usage: workspace-backup.sh
# Designed to run via cron every 6 hours

set -euo pipefail

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_explicit_source() {
  cat >&2 <<'EOF'
ERROR: KAOS_WORKSPACE_SRC is required.

workspace-backup.sh is fail-closed to avoid copying private runtime state into
the public app repository. Configure an explicit private source and target, for
example:

  KAOS_WORKSPACE_SRC=/opt/kaos/app/workspaces/my-agent
  KAOS_BACKUP_REPO_DIR=/srv/private-kaos-workspace-backup
  KAOS_BACKUP_REMOTE=origin
  KAOS_BACKUP_BRANCH=main

The backup target must be a private Git repository. The public app repository
keeps workspace/ ignored as a safety guard.
EOF
  exit 1
}

require_explicit_destination() {
  cat >&2 <<'EOF'
ERROR: KAOS_BACKUP_REPO_DIR is required.

workspace-backup.sh no longer falls back to the public app repository or
silently pushes to origin/main. Configure an explicit private backup Git repo:

  KAOS_BACKUP_REPO_DIR=/srv/private-kaos-workspace-backup
  KAOS_BACKUP_REMOTE=origin
  KAOS_BACKUP_BRANCH=main

Use --dry-run to print the resolved source, target, remote, and branch without
staging or pushing anything.
EOF
  exit 1
}

resolve_dir() {
  local path="$1"
  if [ ! -d "$path" ]; then
    return 1
  fi
  (cd "$path" && pwd -P)
}

sanitize_remote_url() {
  printf '%s' "$1" | sed -E 's#(https?://)[^/@]+@#\1***@#'
}

is_public_code_remote() {
  printf '%s' "$1" | grep -Eqi 'github\.com[:/]spyrae/kronos-agent-os(\.git)?/?$'
}

DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=true
      ;;
    -h|--help)
      cat <<'EOF'
Usage: workspace-backup.sh [--dry-run]

Required configuration:
  KAOS_WORKSPACE_SRC      Explicit Kronos workspace source.
  KAOS_BACKUP_REPO_DIR   Explicit private Git repository for backup storage.

Optional configuration:
  KAOS_BACKUP_REMOTE     Backup Git remote name (default: origin).
  KAOS_BACKUP_BRANCH     Backup Git branch (default: main).
EOF
      exit 0
      ;;
    *)
      fail "unknown argument: $arg"
      ;;
  esac
done

# Resolve the install dir relative to this script (works on any deploy path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load optional local config before resolving backup settings. The follow-up
# ops cleanup will centralize this, but backup safety must not wait for it.
if [ -f "$APP_DIR/.env" ]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/.env" 2>/dev/null || true
fi

[ -n "${KAOS_WORKSPACE_SRC:-}" ] || require_explicit_source
[ -n "${KAOS_BACKUP_REPO_DIR:-}" ] || require_explicit_destination

WORKSPACE_SRC="$KAOS_WORKSPACE_SRC"
REPO_DIR="$KAOS_BACKUP_REPO_DIR"
BACKUP_REMOTE="${KAOS_BACKUP_REMOTE:-origin}"
BACKUP_BRANCH="${KAOS_BACKUP_BRANCH:-main}"

APP_DIR_RESOLVED="$(resolve_dir "$APP_DIR")" || fail "cannot resolve APP_DIR: $APP_DIR"
REPO_DIR_RESOLVED="$(resolve_dir "$REPO_DIR")" || fail "KAOS_BACKUP_REPO_DIR does not exist or is not a directory: $REPO_DIR"
WORKSPACE_SRC_RESOLVED="$(resolve_dir "$WORKSPACE_SRC")" || fail "KAOS_WORKSPACE_SRC does not exist or is not a directory: $WORKSPACE_SRC"

[ -d "$REPO_DIR_RESOLVED/.git" ] || fail "KAOS_BACKUP_REPO_DIR must point to a Git repository: $REPO_DIR_RESOLVED"

if [ "$WORKSPACE_SRC_RESOLVED" = "$APP_DIR_RESOLVED" ] || [ "$WORKSPACE_SRC_RESOLVED" = "$REPO_DIR_RESOLVED" ]; then
  fail "KAOS_WORKSPACE_SRC must not be the app/repo root: $WORKSPACE_SRC_RESOLVED"
fi

if [ "$WORKSPACE_SRC_RESOLVED" = "$APP_DIR_RESOLVED/workspaces" ]; then
  fail "KAOS_WORKSPACE_SRC must point to one explicit private workspace, not the aggregate app/workspaces directory"
fi

if [ "$WORKSPACE_SRC_RESOLVED" = "$APP_DIR_RESOLVED/workspace" ]; then
  fail "KAOS_WORKSPACE_SRC must not be the legacy app/workspace backup target"
fi

BACKUP_TARGET="$REPO_DIR_RESOLVED/workspace"
case "$WORKSPACE_SRC_RESOLVED" in
  "$BACKUP_TARGET"|"$BACKUP_TARGET"/*)
    fail "KAOS_WORKSPACE_SRC must not be inside the backup target: $BACKUP_TARGET"
    ;;
esac

missing_layout=""
for required_dir in self notes ops; do
  if [ ! -d "$WORKSPACE_SRC_RESOLVED/$required_dir" ]; then
    missing_layout="$missing_layout $required_dir/"
  fi
done
if [ -n "$missing_layout" ]; then
  fail "KAOS_WORKSPACE_SRC is not a Kronos workspace; missing:$missing_layout"
fi

cd "$REPO_DIR_RESOLVED"

if [ "$REPO_DIR_RESOLVED" = "$APP_DIR_RESOLVED" ]; then
  if git check-ignore -q "workspace/test.md"; then
    fail "refusing to back up into the public app repository; set KAOS_BACKUP_REPO_DIR to a private backup Git repo"
  fi
  fail "refusing to back up into the app repository because workspace/ is not ignored; add the guard and use a private backup Git repo"
fi

remote_url="$(git remote get-url "$BACKUP_REMOTE" 2>/dev/null || true)"
[ -n "$remote_url" ] || fail "backup remote '$BACKUP_REMOTE' is not configured in $REPO_DIR_RESOLVED"
safe_remote_url="$(sanitize_remote_url "$remote_url")"
if is_public_code_remote "$remote_url"; then
  fail "backup remote points to public code repo ($safe_remote_url); use a private backup repository"
fi

if git check-ignore -q "workspace/test.md"; then
  fail "backup target workspace/ is ignored in KAOS_BACKUP_REPO_DIR; use a private repo where workspace/ is intentionally trackable"
fi

# NTFY config
NTFY_URL="${NTFY_URL:-${NTFY_URL:-https://ntfy.sh}}"
NTFY_TOKEN="${NTFY_TOKEN:-}"
NTFY_TOPIC="${NTFY_TOPIC:-persona-alerts}"

# --- Safety: verify source is populated (Three-Space: self/, notes/, ops/) ---
md_count=$(find "$WORKSPACE_SRC_RESOLVED" -name "*.md" -type f 2>/dev/null | wc -l)
if [ "$md_count" -lt 3 ]; then
  echo "ERROR: workspace source has fewer than 3 .md files ($md_count found). Aborting to prevent data loss."
  exit 1
fi

echo "Backup preflight:"
echo "  source: $WORKSPACE_SRC_RESOLVED"
echo "  backup_repo: $REPO_DIR_RESOLVED"
echo "  backup_target: $BACKUP_TARGET"
echo "  remote: $BACKUP_REMOTE ($safe_remote_url)"
echo "  branch: $BACKUP_BRANCH"
echo "  dry_run: $DRY_RUN"

if [ "$DRY_RUN" = "true" ]; then
  echo "Dry run complete. No rsync, staging, commit, or push performed."
  exit 0
fi

# --- Pull latest first (handle parallel commits from dev machine) ---
git pull --rebase "$BACKUP_REMOTE" "$BACKUP_BRANCH" 2>&1 || true

# --- Stage workspace changes ---
# Workspace is copied into workspace/ relative to the private backup repo.
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.state/' \
  --exclude='.pi/' \
  --exclude='.gitignore' \
  "$WORKSPACE_SRC_RESOLVED/" "$BACKUP_TARGET/"

# --- Check for changes ---
git add workspace/

CHANGES=$(git diff --cached --stat)

if [ -z "$CHANGES" ]; then
  echo "No workspace changes to backup."
  exit 0
fi

# --- Count changed files ---
files_changed=$(git diff --cached --numstat | wc -l | tr -d ' ')

# --- Commit and push ---
timestamp=$(date '+%Y-%m-%d %H:%M UTC')
git commit -m "auto: workspace backup $timestamp

Files changed: $files_changed"

if git push "$BACKUP_REMOTE" "HEAD:$BACKUP_BRANCH" 2>&1; then
  echo "Backup pushed successfully."

  # NTFY notification
  if [ -n "$NTFY_TOKEN" ]; then
    msg=$(printf "Workspace backup pushed\n\nTime: %s\nFiles changed: %s\nRemote: %s\nBranch: %s" "$timestamp" "$files_changed" "$safe_remote_url" "$BACKUP_BRANCH")
    curl -s -d "$msg" \
      -H "Title: Kronos Agent OS Backup OK" \
      -H "Priority: low" \
      -H "Tags: white_check_mark,floppy_disk" \
      -H "Authorization: Bearer $NTFY_TOKEN" \
      "$NTFY_URL/$NTFY_TOPIC" > /dev/null 2>&1
  fi
else
  echo "ERROR: git push failed"

  # Alert on push failure
  if [ -n "$NTFY_TOKEN" ]; then
    msg=$(printf "Workspace backup FAILED: git push error\n\nRemote: %s\nBranch: %s" "$safe_remote_url" "$BACKUP_BRANCH")
    curl -s -d "$msg" \
      -H "Title: Kronos Agent OS Backup FAILED" \
      -H "Priority: high" \
      -H "Tags: warning,floppy_disk" \
      -H "Authorization: Bearer $NTFY_TOKEN" \
      "$NTFY_URL/$NTFY_TOPIC" > /dev/null 2>&1
  fi
  exit 1
fi
