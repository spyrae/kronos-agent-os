#!/bin/bash
# workspace-backup.sh — Auto-backup workspace files to Git
# Commits workspace changes in the app repo and pushes.
#
# Usage: workspace-backup.sh
# Designed to run via cron every 6 hours

set -uo pipefail

# Resolve the install dir relative to this script (works on any deploy path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Git repo to commit into = the app dir; workspace source defaults to the
# per-agent workspaces dir. Override either via env.
REPO_DIR="${KAOS_REPO_DIR:-$APP_DIR}"
WORKSPACE_SRC="${KAOS_WORKSPACE_SRC:-$APP_DIR/workspaces}"

# NTFY config
if [ -f "$APP_DIR/.env" ]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/.env" 2>/dev/null || true
fi
NTFY_URL="${NTFY_URL:-${NTFY_URL:-https://ntfy.sh}}"
NTFY_TOKEN="${NTFY_TOKEN:-}"
NTFY_TOPIC="${NTFY_TOPIC:-persona-alerts}"

# --- Safety: verify source is populated (Three-Space: self/, notes/, ops/) ---
md_count=$(find "$WORKSPACE_SRC" -name "*.md" -type f 2>/dev/null | wc -l)
if [ "$md_count" -lt 3 ]; then
  echo "ERROR: workspace source has fewer than 3 .md files ($md_count found). Aborting to prevent data loss."
  exit 1
fi

# --- Pull latest first (handle parallel commits from dev machine) ---
cd "$REPO_DIR"
git pull --rebase origin main 2>&1 || true

# --- Stage workspace changes ---
# Workspace is at ../workspace relative to repo, copy into repo for commit
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.state/' \
  --exclude='.pi/' \
  --exclude='.gitignore' \
  "$WORKSPACE_SRC/" "$REPO_DIR/workspace/"

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

if git push origin main 2>&1; then
  echo "Backup pushed successfully."

  # NTFY notification
  if [ -n "$NTFY_TOKEN" ]; then
    msg=$(printf "Workspace backup pushed\n\nTime: %s\nFiles changed: %s\n\n%s" "$timestamp" "$files_changed" "$CHANGES")
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
    curl -s -d "Workspace backup FAILED: git push error" \
      -H "Title: Kronos Agent OS Backup FAILED" \
      -H "Priority: high" \
      -H "Tags: warning,floppy_disk" \
      -H "Authorization: Bearer $NTFY_TOKEN" \
      "$NTFY_URL/$NTFY_TOPIC" > /dev/null 2>&1
  fi
  exit 1
fi
