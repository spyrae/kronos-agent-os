"""Packaging guard: every shipped data file is declared and loadable.

The wheel bundles non-.py assets (YAML/JSON/MD) that the runtime loads from
package-relative paths. If an asset is loaded from inside the package but not
declared in ``[tool.setuptools.package-data]``, ``pip install`` yields a wheel
that imports yet breaks at runtime. These tests fail fast on that class of
regression without building a wheel; ``scripts/smoke_wheel.sh`` does the full
build → install → import check.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[1]


def _package_data() -> dict[str, list[str]]:
    data = tomllib.loads((APP_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]


def test_every_declared_package_data_glob_matches_a_file():
    for package, patterns in _package_data().items():
        pkg_dir = APP_ROOT / package.replace(".", "/")
        assert pkg_dir.is_dir(), f"package-data names a missing package dir: {package}"
        for pattern in patterns:
            assert list(pkg_dir.glob(pattern)), (
                f"package-data {package!r} pattern {pattern!r} matches no file — the wheel would ship without it"
            )


def test_shipped_assets_exist_and_parse():
    # The assets the runtime actually loads from inside the package. Each must
    # be present (→ in the wheel) and well-formed.
    competitors = APP_ROOT / "kronos" / "competitors" / "competitors.yaml"
    sources = APP_ROOT / "kronos" / "signals" / "SOURCES.yaml"
    keywords = APP_ROOT / "aso" / "config" / "keywords.json"

    assert yaml.safe_load(competitors.read_text(encoding="utf-8"))
    assert yaml.safe_load(sources.read_text(encoding="utf-8"))
    assert json.loads(keywords.read_text(encoding="utf-8"))
    for md in ("analyst.md", "evaluator.md", "strategist.md"):
        assert (APP_ROOT / "aso" / "prompts" / md).read_text(encoding="utf-8").strip()


def test_user_config_loaders_degrade_without_their_yaml(monkeypatch, tmp_path):
    # agents.yaml / servers.yaml are user-config (gitignored, not shipped in the
    # wheel). Their loaders must degrade to empty, not crash, on a fresh install.
    monkeypatch.setenv("AGENTS_CONFIG_PATH", str(tmp_path / "absent-agents.yaml"))
    monkeypatch.setenv("SERVER_REGISTRY_PATH", str(tmp_path / "absent-servers.yaml"))

    from kronos.group_router import _load_profiles
    from kronos.tools.server_ops import _load_registry

    assert _load_profiles() == {}
    assert _load_registry() == {}
