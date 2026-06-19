import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECALL = ROOT / "scripts" / "recall.py"


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in ["AUDIT_LOG", "DB_PATH", "KAOS_ENV_FILE", "KRONOS_ENV_FILE", "RECALL_LOG"]:
        env.pop(name, None)
    env["KAOS_ENV_FILE"] = str(tmp_path / "missing.env")
    return env


def _run_recall(
    tmp_path: Path,
    *args: str,
    audit_log: Path,
    db_path: Path,
) -> subprocess.CompletedProcess[str]:
    env = _base_env(tmp_path)
    env.update(
        {
            "AUDIT_LOG": str(audit_log),
            "DB_PATH": str(db_path),
            "RECALL_LOG": str(tmp_path / "logs" / "recall.log"),
        }
    )
    return subprocess.run(
        [sys.executable, str(RECALL), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_audit_log(path: Path, user_text: str, assistant_text: str) -> None:
    path.write_text(
        json.dumps(
            {
                "ts": "2026-06-19T00:00:00Z",
                "input_preview": user_text,
                "output_preview": assistant_text,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _indexed_contents(db_path: Path) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT content FROM messages ORDER BY id",
            ).fetchall()
        ]


def test_missing_audit_log_does_not_delete_existing_index(tmp_path: Path):
    db_path = tmp_path / "recall.db"
    initial_audit = tmp_path / "audit-initial.jsonl"
    _write_audit_log(initial_audit, "Old user message", "Old assistant reply")

    initial = _run_recall(tmp_path, "index", audit_log=initial_audit, db_path=db_path)
    assert initial.returncode == 0, initial.stderr
    assert _indexed_contents(db_path) == ["Old user message", "Old assistant reply"]

    missing_audit = tmp_path / "missing-audit.jsonl"
    result = _run_recall(tmp_path, "index", audit_log=missing_audit, db_path=db_path)

    assert result.returncode == 2
    assert "Audit log not found" in result.stderr
    assert "Set AUDIT_LOG" in result.stderr
    assert _indexed_contents(db_path) == ["Old user message", "Old assistant reply"]


def test_empty_audit_log_requires_force_empty(tmp_path: Path):
    db_path = tmp_path / "recall.db"
    initial_audit = tmp_path / "audit-initial.jsonl"
    empty_audit = tmp_path / "audit-empty.jsonl"
    _write_audit_log(initial_audit, "Keep this message", "Keep this reply")
    empty_audit.write_text("", encoding="utf-8")

    initial = _run_recall(tmp_path, "index", audit_log=initial_audit, db_path=db_path)
    assert initial.returncode == 0, initial.stderr

    result = _run_recall(tmp_path, "index", audit_log=empty_audit, db_path=db_path)

    assert result.returncode == 2
    assert "contains no indexable records" in result.stderr
    assert _indexed_contents(db_path) == ["Keep this message", "Keep this reply"]


def test_force_empty_allows_intentional_empty_rebuild(tmp_path: Path):
    db_path = tmp_path / "recall.db"
    initial_audit = tmp_path / "audit-initial.jsonl"
    empty_audit = tmp_path / "audit-empty.jsonl"
    _write_audit_log(initial_audit, "Remove this message", "Remove this reply")
    empty_audit.write_text("", encoding="utf-8")

    initial = _run_recall(tmp_path, "index", audit_log=initial_audit, db_path=db_path)
    assert initial.returncode == 0, initial.stderr

    result = _run_recall(tmp_path, "index", "--force-empty", audit_log=empty_audit, db_path=db_path)

    assert result.returncode == 0, result.stderr
    assert _indexed_contents(db_path) == []
