import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_common.sh"
LOG_RESOLVER = ROOT / "scripts" / "_log_resolver.sh"


def _run_bash(script: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _base_env(app_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in [
        "AGENT_NAME",
        "DB_DIR",
        "DB_PATH",
        "KAOS_AGENT_NAME",
        "KAOS_APP_DIR",
        "KAOS_COMMON_INITIALIZED",
        "KAOS_ENV_INITIALIZED",
        "KAOS_HEALTH_UNIT",
        "KAOS_LOG_DIR",
        "KAOS_LOG_MODE",
        "KAOS_MAIN_UNIT",
        "NTFY_TOKEN",
        "NTFY_TOPIC",
        "NTFY_URL",
        "WORKSPACE_PATH",
    ]:
        env.pop(name, None)
    env["KAOS_APP_DIR"] = str(app_dir)
    return env


def test_common_loads_env_without_overriding_process_env(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text(
        "\n".join(
            [
                "NTFY_URL=https://env-file.example",
                "NTFY_TOPIC=from-env-file",
                "KAOS_MAIN_UNIT=file-main",
                "WORKSPACE_PATH=workspaces/from-env-file",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = _base_env(app)
    env["NTFY_URL"] = "https://process.example"

    script = f"""
set -euo pipefail
source {shlex.quote(str(COMMON))}
kaos_common_init
printf '%s\\n' "$KAOS_APP_DIR"
printf '%s\\n' "$NTFY_URL"
printf '%s\\n' "$NTFY_TOPIC"
printf '%s\\n' "$KAOS_MAIN_UNIT_RESOLVED"
printf '%s\\n' "$KAOS_WORKSPACE_PATH_RESOLVED"
"""
    result = _run_bash(script, env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(app),
        "https://process.example",
        "from-env-file",
        "file-main",
        str(app / "workspaces" / "from-env-file"),
    ]


def test_common_defaults_work_from_any_cwd(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    cwd = tmp_path / "other"
    cwd.mkdir()
    env = _base_env(app)

    script = f"""
set -euo pipefail
source {shlex.quote(str(COMMON))}
kaos_common_init
printf '%s\\n' "$NTFY_URL"
printf '%s\\n' "$NTFY_TOPIC"
printf '%s\\n' "$KAOS_MAIN_UNIT_RESOLVED"
printf '%s\\n' "$KAOS_HEALTH_UNIT_RESOLVED"
printf '%s\\n' "$KAOS_WORKSPACE_PATH_RESOLVED"
"""
    result = _run_bash(script, env, cwd=cwd)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://ntfy.sh",
        "persona-alerts",
        "kaos",
        "kronos-health.service",
        str(app / "workspaces" / "kronos"),
    ]


def test_log_resolver_uses_common_env_precedence(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("DB_PATH=data/from-env/session.db\n", encoding="utf-8")
    env = _base_env(app)
    env["DB_PATH"] = "data/from-process/session.db"

    script = f"""
set -euo pipefail
source {shlex.quote(str(LOG_RESOLVER))}
kaos_resolve_log_sources
printf '%s\\n' "${{KAOS_LOG_LABELS[0]}}"
printf '%s\\n' "${{KAOS_LOG_REASONS[0]}}"
printf '%s\\n' "${{KAOS_LOG_DIRS[0]}}"
"""
    result = _run_bash(script, env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "from-process",
        "DB_PATH",
        str(app / "data" / "from-process" / "logs"),
    ]
