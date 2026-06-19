import os
import shlex
import subprocess
from pathlib import Path

from kronos.ops.logs import resolve_log_sources

ROOT = Path(__file__).resolve().parents[1]


def test_python_log_resolver_defaults_to_agent_data_logs(tmp_path: Path) -> None:
    app = tmp_path / "app"

    result = resolve_log_sources(app_dir=app, env={})

    assert result.mode == "single"
    assert result.sources[0].label == "kronos"
    assert result.sources[0].reason == "AGENT_NAME"
    assert result.sources[0].path == app / "data" / "kronos" / "logs"


def test_python_log_resolver_prefers_db_path(tmp_path: Path) -> None:
    app = tmp_path / "app"

    result = resolve_log_sources(app_dir=app, env={"DB_PATH": "data/worker/session.db"})

    assert result.mode == "single"
    assert result.sources[0].label == "worker"
    assert result.sources[0].reason == "DB_PATH"
    assert result.sources[0].path == app / "data" / "worker" / "logs"


def test_python_log_resolver_aggregates_existing_agent_logs(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "data" / "alpha" / "logs").mkdir(parents=True)
    (app / "data" / "beta" / "logs").mkdir(parents=True)

    result = resolve_log_sources(app_dir=app, env={"KAOS_LOG_MODE": "aggregate"})

    assert result.mode == "aggregate"
    assert [source.label for source in result.sources] == ["alpha", "beta"]
    assert result.warnings == ()


def test_shell_log_resolver_matches_default_topology(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    env = os.environ.copy()
    env.update({"KAOS_APP_DIR": str(app), "AGENT_NAME": "kronos"})
    env.pop("KAOS_LOG_DIR", None)
    env.pop("KAOS_LOG_MODE", None)
    env.pop("KAOS_AGENT_NAME", None)
    env.pop("DB_PATH", None)
    env.pop("DB_DIR", None)

    resolver = shlex.quote(str(ROOT / "scripts" / "_log_resolver.sh"))
    script = f"""
set -euo pipefail
source {resolver}
kaos_resolve_log_sources
printf '%s\\n' "$KAOS_LOG_MODE_RESOLVED"
printf '%s\\n' "${{KAOS_LOG_LABELS[0]}}"
printf '%s\\n' "${{KAOS_LOG_REASONS[0]}}"
printf '%s\\n' "${{KAOS_LOG_DIRS[0]}}"
"""
    result = subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)

    assert result.stdout.splitlines() == [
        "single",
        "kronos",
        "AGENT_NAME",
        str(app / "data" / "kronos" / "logs"),
    ]


def test_shell_log_resolver_aggregates_existing_agent_logs(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "data" / "alpha" / "logs").mkdir(parents=True)
    (app / "data" / "beta" / "logs").mkdir(parents=True)
    env = os.environ.copy()
    env.update({"KAOS_APP_DIR": str(app), "KAOS_LOG_MODE": "aggregate"})
    for name in ("KAOS_LOG_DIR", "KAOS_AGENT_NAME", "AGENT_NAME", "DB_PATH", "DB_DIR"):
        env.pop(name, None)

    resolver = shlex.quote(str(ROOT / "scripts" / "_log_resolver.sh"))
    script = f"""
set -euo pipefail
source {resolver}
kaos_resolve_log_sources
printf '%s\\n' "$KAOS_LOG_MODE_RESOLVED"
for i in "${{!KAOS_LOG_DIRS[@]}}"; do
  printf '%s=%s:%s\\n' "${{KAOS_LOG_LABELS[$i]}}" "${{KAOS_LOG_REASONS[$i]}}" "${{KAOS_LOG_DIRS[$i]}}"
done
"""
    result = subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)

    assert result.stdout.splitlines() == [
        "aggregate",
        f"alpha=aggregate:data/*/logs:{app / 'data' / 'alpha' / 'logs'}",
        f"beta=aggregate:data/*/logs:{app / 'data' / 'beta' / 'logs'}",
    ]
