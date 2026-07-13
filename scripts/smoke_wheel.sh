#!/bin/bash
# smoke_wheel.sh — build the wheel, install it into a throwaway venv, and prove
# the installed package imports and finds its bundled data files.
#
# Usage: scripts/smoke_wheel.sh   (run from the app/ directory)
#
# Why: the wheel ships non-.py assets (competitors.yaml, SOURCES.yaml, aso
# config/prompts). A `pip install` that omits them imports fine but breaks at
# runtime. This is the end-to-end guard; tests/test_packaging.py is the fast,
# build-free version that runs in CI.
#
# Requires: uv. The check runs from a temp dir (NOT the repo) so assets must
# resolve from site-packages, not the source tree.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> building wheel"
uv build --wheel --out-dir "$work/dist" >/dev/null
whl="$(ls -t "$work"/dist/*.whl | head -1)"
echo "    $(basename "$whl")"

echo "==> installing into a fresh venv"
uv venv "$work/venv" >/dev/null
uv pip install --python "$work/venv/bin/python" "$whl" >/dev/null

echo "==> importing + loading bundled assets (cwd outside the repo)"
cd "$work"
"$work/venv/bin/python" - <<'PY'
import json
from pathlib import Path

import yaml

import kronos  # noqa: F401

# competitors.yaml — loaded via Path(__file__).parent; must come from the wheel.
from kronos.competitors.config import _YAML_PATH
assert _YAML_PATH.exists(), f"missing {_YAML_PATH}"
assert "site-packages" in str(_YAML_PATH), f"not from installed package: {_YAML_PATH}"
assert yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))

# SOURCES.yaml — packaged path resolves next to the module.
import kronos.signals.sources as sources
packaged = Path(sources.__file__).with_name("SOURCES.yaml")
assert packaged.exists(), f"missing {packaged}"
yaml.safe_load(packaged.read_text(encoding="utf-8"))

# aso config + prompts.
import aso
aso_dir = Path(aso.__file__).parent
json.loads((aso_dir / "config" / "keywords.json").read_text(encoding="utf-8"))
for md in ("analyst.md", "evaluator.md", "strategist.md"):
    assert (aso_dir / "prompts" / md).exists(), md

# dashboard imports without a built UI (dashboard-ui/dist is not in the wheel).
import dashboard.server  # noqa: F401

# user-config loaders degrade gracefully when their (unshipped) YAML is absent.
from kronos.group_router import _load_profiles
_load_profiles()

print("assets: competitors.yaml, SOURCES.yaml, aso config+prompts — all load from site-packages")
PY

echo "==> SMOKE PASS: wheel installs, imports, and ships its data files"
