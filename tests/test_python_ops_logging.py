import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTACT_PROFILER = ROOT / "scripts" / "contact-profiler.py"
RECALL = ROOT / "scripts" / "recall.py"


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in [
        "AGENT_NAME",
        "AUDIT_LOG",
        "DB_PATH",
        "KAOS_ENV_FILE",
        "KAOS_WORKSPACE_PATH",
        "KRONOS_ENV_FILE",
        "PROFILER_LOG",
        "RECALL_LOG",
        "WORKSPACE",
        "WORKSPACE_PATH",
    ]:
        env.pop(name, None)
    env["KAOS_ENV_FILE"] = str(tmp_path / "missing.env")
    return env


def _run_script(
    tmp_path: Path,
    script: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _base_env(tmp_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_audit_log(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "ts": "2026-06-19T00:00:00Z",
                "tier": "lite",
                "input_preview": "Find the current project status",
                "output_preview": "The project status is green",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_contact_profiler_help_does_not_touch_profiler_log(tmp_path: Path):
    log_file = tmp_path / "missing-log-dir" / "contact-profiler.log"
    result = _run_script(
        tmp_path,
        CONTACT_PROFILER,
        "--help",
        extra_env={"PROFILER_LOG": str(log_file)},
    )

    assert result.returncode == 0, result.stderr
    assert "Contact Profiler" in result.stdout
    assert not log_file.parent.exists()


def test_recall_stats_does_not_touch_recall_log(tmp_path: Path):
    log_file = tmp_path / "missing-log-dir" / "recall.log"
    result = _run_script(
        tmp_path,
        RECALL,
        "stats",
        extra_env={
            "DB_PATH": str(tmp_path / "missing.db"),
            "RECALL_LOG": str(log_file),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "No index found" in result.stdout
    assert not log_file.parent.exists()


def test_recall_index_writes_explicit_log_when_enabled(tmp_path: Path):
    audit_log = tmp_path / "audit.jsonl"
    db_path = tmp_path / "recall.db"
    log_file = tmp_path / "logs" / "recall.log"
    _write_audit_log(audit_log)

    result = _run_script(
        tmp_path,
        RECALL,
        "index",
        extra_env={
            "AUDIT_LOG": str(audit_log),
            "DB_PATH": str(db_path),
            "RECALL_LOG": str(log_file),
        },
    )

    assert result.returncode == 0, result.stderr
    assert log_file.is_file()
    assert "Index built: 2 total messages" in log_file.read_text(encoding="utf-8")


def test_recall_index_falls_back_when_file_logging_is_unavailable(tmp_path: Path):
    audit_log = tmp_path / "audit.jsonl"
    db_path = tmp_path / "recall.db"
    bad_parent = tmp_path / "not-a-directory"
    bad_parent.write_text("occupied", encoding="utf-8")
    _write_audit_log(audit_log)

    result = _run_script(
        tmp_path,
        RECALL,
        "index",
        extra_env={
            "AUDIT_LOG": str(audit_log),
            "DB_PATH": str(db_path),
            "RECALL_LOG": str(bad_parent / "recall.log"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "file logging disabled" in result.stderr
    assert db_path.is_file()


def test_contact_profiler_setup_logging_writes_explicit_log(tmp_path: Path):
    log_file = tmp_path / "logs" / "contact-profiler.log"
    env = _base_env(tmp_path)
    env["PROFILER_LOG"] = str(log_file)
    code = f"""
import importlib.util

spec = importlib.util.spec_from_file_location("contact_profiler_under_test", {str(CONTACT_PROFILER)!r})
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
module.setup_logging()
module.log.info("contact profiler log ready")
print(module.resolve_log_file())
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == log_file
    assert "contact profiler log ready" in log_file.read_text(encoding="utf-8")
