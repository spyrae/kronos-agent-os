#!/usr/bin/env bash
# setup-stealth.sh — install the optional stealth fetch backend.
#
# Usage: setup-stealth.sh [install-dir]
#
# The middle acquisition tier (kronos/tools/acquire.py) shells out to a stealth
# browser for sites that refuse an ordinary GET. That browser is deliberately
# NOT a dependency of this package: it is a ~150 MB binary, most deployments do
# not need it, and a missing backend is a reported skipped tier rather than an
# error. This script installs it, verifies it, and prints the one line that
# wires it in.
#
# It installs OUTSIDE the app directory on purpose. deploy.sh rsyncs `app/` with
# --delete, so anything placed under it is erased on the next deploy — a venv
# there would work exactly once. The adapter (scripts/stealth_fetch.py) stays in
# the repo and ships with every deploy; only the backend lives outside it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
DEFAULT_DIR="$(dirname "$APP_DIR")/tools"
INSTALL_DIR="${1:-${KAOS_STEALTH_DIR:-$DEFAULT_DIR}}"

VENV="$INSTALL_DIR/.venv"
PYTHON="$VENV/bin/python"
FETCHER="$APP_DIR/scripts/stealth_fetch.py"

fail() {
  echo "FATAL: $*" >&2
  exit 1
}

case "$INSTALL_DIR" in
  "$APP_DIR"|"$APP_DIR"/*)
    fail "refusing to install into $INSTALL_DIR: deploy.sh rsyncs app/ with --delete and would erase it on the next deploy."
    ;;
esac

echo "Installing the stealth backend into $INSTALL_DIR"

mkdir -p "$INSTALL_DIR"

if [ -d "$VENV" ] && [ ! -x "$PYTHON" ]; then
  echo "Removing an incomplete virtualenv from a previous run."
  rm -rf "$VENV"
fi

if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$VENV" || fail "could not create a virtualenv at $VENV (is python3-venv installed?)"
fi

# Only cloakbrowser. The AI/parsing extras of the wider scraping stack pull in
# hundreds of megabytes and this tier never parses — acquire.py wants the raw
# document and does its own extraction.
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet cloakbrowser || fail "could not install cloakbrowser"

echo "Downloading the stealth browser binary (skipped when already cached)..."
"$VENV/bin/cloakbrowser" install || fail "could not download the stealth browser binary"

# Prove it before claiming it works. An install that imports but cannot open a
# page is the failure this whole exercise exists to stop being silent about.
echo "Verifying against example.com..."
if ! "$PYTHON" "$FETCHER" https://example.com >/dev/null; then
  fail "the backend installed but could not fetch a page. Run '$VENV/bin/cloakbrowser info' for diagnostics."
fi

cat <<EOF

Done. Add this to the agent's .env and restart the agents:

STEALTH_FETCH_COMMAND=$PYTHON $FETCHER {url}

Then confirm the tier is live:

  .venv/bin/python -m kronos.cli acquire check
EOF
