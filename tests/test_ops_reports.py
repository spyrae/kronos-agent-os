import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _clean_env(app_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "KAOS_LOG_DIR",
        "KAOS_LOG_MODE",
        "KAOS_AGENT_NAME",
        "KAOS_WORKSPACE_SRC",
        "AGENT_NAME",
        "DB_PATH",
        "DB_DIR",
        "NTFY_TOKEN",
        "WORKSPACE_PATH",
    ):
        env.pop(name, None)
    env["KAOS_APP_DIR"] = str(app_dir)
    return env


def test_cost_stats_reads_default_agent_log_dir_without_manual_override(tmp_path: Path) -> None:
    app = tmp_path / "app"
    today = datetime.now(UTC).date().isoformat()
    old = (datetime.now(UTC).date() - timedelta(days=8)).isoformat()
    logs = app / "data" / "kronos" / "logs"
    _write_jsonl(
        logs / "cost.jsonl",
        [
            {"ts": f"{today}T01:00:00+0000", "tier": "lite", "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.01},
            {
                "ts": f"{old}T01:00:00+0000",
                "tier": "standard",
                "input_tokens": 30,
                "output_tokens": 40,
                "cost_usd": 0.02,
            },
        ],
    )
    _write_jsonl(
        logs / "audit.jsonl",
        [
            {"ts": f"{today}T01:00:00+0000", "blocked": True},
            {"ts": f"{old}T01:00:00+0000", "blocked": True},
        ],
    )
    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "cost-stats.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"{logs}/cost.jsonl" in result.stdout
    assert "Total: 1 requests, $0.0100" in result.stdout
    assert "Blocked requests (today): 1" in result.stdout


def test_cost_stats_aggregates_agent_log_dirs(tmp_path: Path) -> None:
    app = tmp_path / "app"
    today = datetime.now(UTC).date().isoformat()
    _write_jsonl(
        app / "data" / "alpha" / "logs" / "cost.jsonl",
        [{"ts": f"{today}T01:00:00+0000", "tier": "lite", "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.01}],
    )
    _write_jsonl(
        app / "data" / "beta" / "logs" / "cost.jsonl",
        [
            {
                "ts": f"{today}T02:00:00+0000",
                "tier": "standard",
                "input_tokens": 30,
                "output_tokens": 40,
                "cost_usd": 0.02,
            }
        ],
    )
    env = _clean_env(app)
    env["KAOS_LOG_MODE"] = "aggregate"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "cost-stats.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Requests by source:" in result.stdout
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
    assert "Total: 2 requests, $0.0300" in result.stdout


def test_cost_stats_missing_logs_is_explicit_not_empty_success(tmp_path: Path) -> None:
    app = tmp_path / "app"
    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "cost-stats.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "No cost log found for resolved sources:" in result.stdout
    assert f"{app}/data/kronos/logs/cost.jsonl" in result.stdout
    assert "No requests found for this period" not in result.stdout


def test_ops_help_and_stats_do_not_require_var_log(tmp_path: Path) -> None:
    app = tmp_path / "app"
    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"

    commands = [
        ["bash", str(ROOT / "scripts" / "workspace-backup.sh"), "--help"],
        ["bash", str(ROOT / "scripts" / "cost-stats.sh"), "today"],
        ["bash", str(ROOT / "scripts" / "security-audit.sh"), "today"],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        combined_output = result.stdout + result.stderr
        assert "/var/log" not in combined_output
        assert "Permission denied" not in combined_output


def _fake_command(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_daily_status_reports_resolved_audit_source(tmp_path: Path) -> None:
    app = tmp_path / "app"
    logs = app / "data" / "kronos" / "logs"
    today = datetime.now(UTC).date().isoformat()
    _write_jsonl(logs / "audit.jsonl", [{"ts": f"{today}T01:00:00+0000"}])

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_command(fake_bin, "systemctl", 'if [ "$1" = "is-active" ]; then echo active; exit 0; fi\necho n/a\n')
    _fake_command(fake_bin, "curl", "exit 1\n")
    _fake_command(fake_bin, "uptime", 'echo "up 1 day"\n')
    _fake_command(fake_bin, "df", 'printf "Filesystem Size Used Avail Use%% Mounted\\n/dev/disk 10G 1G 9G 10%% /\\n"\n')
    _fake_command(fake_bin, "free", 'printf "      total used free\\nMem:  10G  2G   8G\\n"\n')
    _fake_command(
        fake_bin, "journalctl", 'printf "Jun 19 All checks passed.\\nJun 19 FAIL: Bridge /health not responding\\n"\n'
    )
    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "daily-status.sh")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "1 requests today" in result.stdout
    assert "Source: single: 1 source(s)" in result.stdout
    assert "Unit: kronos-health.service" in result.stdout
    assert "Runs: 2" in result.stdout
    assert "Failures: 1" in result.stdout


def test_security_audit_uses_snake_case_fields_and_period_filter(tmp_path: Path) -> None:
    app = tmp_path / "app"
    logs = app / "data" / "kronos" / "logs"
    today = datetime.now(UTC).date().isoformat()
    old = (datetime.now(UTC).date() - timedelta(days=8)).isoformat()
    _write_jsonl(
        logs / "audit.jsonl",
        [
            {
                "ts": f"{today}T01:00:00+0000",
                "blocked": True,
                "tier": "lite",
                "approx_cost_usd": 0.01,
                "duration_ms": 2000,
            },
            {
                "ts": f"{old}T01:00:00+0000",
                "blocked": False,
                "tier": "standard",
                "approx_cost_usd": 0.99,
                "duration_ms": 9000,
            },
        ],
    )
    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "security-audit.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Total requests: 1" in result.stdout
    assert "Blocked: 1 (100.0%)" in result.stdout
    assert "Total cost: $0.0100" in result.stdout
    assert "Avg response time: 2.0s" in result.stdout
    assert "standard" not in result.stdout


def test_security_audit_marks_dead_logs_as_not_implemented(tmp_path: Path) -> None:
    app = tmp_path / "app"
    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "security-audit.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "security.jsonl not implemented/configured" in result.stdout
    assert "audit.jsonl missing / not configured" in result.stdout
    assert "router-cost.jsonl: not implemented/configured" in result.stdout
    assert "Exposed secrets in workspace: not checked (missing directory:" in result.stdout


def test_security_audit_uses_runtime_workspace_path_not_backup_source(tmp_path: Path) -> None:
    app = tmp_path / "app"
    runtime_workspace = app / "workspaces" / "kronos"
    backup_source = tmp_path / "backup-source"
    runtime_workspace.mkdir(parents=True)
    backup_source.mkdir(parents=True)
    (backup_source / "secret.md").write_text("fake sk-proj test token", encoding="utf-8")

    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"
    env["WORKSPACE_PATH"] = "workspaces/kronos"
    env["KAOS_WORKSPACE_SRC"] = str(backup_source)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "security-audit.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Exposed secrets in workspace: OK (none found)" in result.stdout


def test_security_audit_resolves_relative_runtime_workspace_path(tmp_path: Path) -> None:
    app = tmp_path / "app"
    runtime_workspace = app / "workspaces" / "kronos"
    runtime_workspace.mkdir(parents=True)
    (runtime_workspace / "secret.md").write_text("fake sk-proj test token", encoding="utf-8")

    env = _clean_env(app)
    env["AGENT_NAME"] = "kronos"
    env["WORKSPACE_PATH"] = "workspaces/kronos"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "security-audit.sh"), "today"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Exposed secrets in workspace: WARNING: 1 files contain API keys!" in result.stdout
