"""CLI surface for eval capture/run/diff/turns (moat phase 8.6)."""

import json

import pytest
import yaml

from kronos.cli import build_parser, main
from kronos.config import settings


def _scenario_dir(tmp_path, name="demo", expect=None, script=None):
    directory = tmp_path / "suite" / name
    directory.mkdir(parents=True)
    (directory / "scenario.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": name,
                "draft": False,
                "input": "проверь статус",
                "script": script or [{"tool_calls": [{"name": "get_status", "args": {}}]}, {"content": "всё ок"}],
                "tool_outputs": {"get_status": ["healthy"]},
                "expect": expect if expect is not None else {"tools_called": ["get_status"]},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return tmp_path / "suite"


def test_parser_exposes_eval_commands():
    parser = build_parser()

    assert parser.parse_args(["eval", "run", "--suite", "s", "--json", "r.json"]).eval_command == "run"
    captured = parser.parse_args(["eval", "capture", "--turn", "t1", "--allow-pii"])
    assert captured.turn == "t1" and captured.allow_pii is True
    diffed = parser.parse_args(["eval", "diff", "--base", "main", "--base-json", "b.json"])
    assert diffed.base == "main" and diffed.base_json == "b.json"
    assert parser.parse_args(["eval", "turns", "--limit", "5"]).limit == 5


def test_eval_run_passes_and_writes_a_report(tmp_path, capsys):
    suite = _scenario_dir(tmp_path)
    report = tmp_path / "out" / "report.json"

    code = main(["eval", "run", "--suite", str(suite), "--json", str(report)])
    out = capsys.readouterr().out

    assert code == 0
    assert "[PASS] demo" in out
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["passed"] == 1 and payload["failed"] == 0


def test_eval_run_fails_on_a_failing_scenario(tmp_path, capsys):
    suite = _scenario_dir(tmp_path, expect={"tools_forbidden": ["get_status"]})

    code = main(["eval", "run", "--suite", str(suite)])

    assert code == 1
    assert "[FAIL] demo" in capsys.readouterr().out


def test_eval_run_on_missing_suite_is_explicit(tmp_path, capsys):
    code = main(["eval", "run", "--suite", str(tmp_path / "nope")])

    assert code == 1
    assert "No scenario suite at" in capsys.readouterr().out


def test_eval_run_reports_an_empty_suite_as_failure(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()

    code = main(["eval", "run", "--suite", str(empty)])

    assert code == 1
    assert "nothing was verified" in capsys.readouterr().out


def test_eval_diff_against_a_saved_report(tmp_path, capsys):
    suite = _scenario_dir(tmp_path)
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                "suite": "suite",
                "scenarios": [
                    {
                        "name": "demo",
                        "status": "pass",
                        "metrics": {
                            "tools_called": [],
                            "approvals_requested": [],
                            "model_turns": 2,
                            "tool_calls": 0,
                            "answer_chars": 6,
                        },
                        "checks": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code = main(["eval", "diff", "--suite", str(suite), "--base-json", str(base)])
    out = capsys.readouterr().out

    assert code == 0  # a behaviour change is information, not a failure
    assert "tools_changed" in out
    assert "get_status" in out


def test_eval_diff_flags_a_new_failure_with_exit_code(tmp_path, capsys):
    suite = _scenario_dir(tmp_path, expect={"tools_forbidden": ["get_status"]})
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps({"suite": "suite", "scenarios": [{"name": "demo", "status": "pass", "metrics": {}, "checks": []}]}),
        encoding="utf-8",
    )

    code = main(["eval", "diff", "--suite", str(suite), "--base-json", str(base)])

    assert code == 1
    assert "new_failure" in capsys.readouterr().out


def test_eval_diff_returns_2_when_it_cannot_compare(tmp_path, capsys):
    """An unusable base is not the author's regression, so it must not gate CI."""
    suite = _scenario_dir(tmp_path)

    code = main(["eval", "diff", "--suite", str(suite), "--base-json", str(tmp_path / "absent.json")])

    assert code == 2
    assert "Diff unavailable:" in capsys.readouterr().out


def test_eval_capture_requires_a_target(tmp_path, capsys):
    code = main(["eval", "capture", "--suite", str(tmp_path)])

    assert code == 1
    assert "Pass --turn" in capsys.readouterr().out


@pytest.fixture
def journal(tmp_path, monkeypatch):
    import sqlite3

    db_dir = tmp_path / "data" / "cli-eval"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "session.db"
    monkeypatch.setattr(settings, "agent_name", "cli-eval")
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE active_turns (turn_id TEXT PRIMARY KEY, thread_id TEXT, status TEXT,
            input_message TEXT, started_at TIMESTAMP, completed_at TIMESTAMP, error TEXT);
        CREATE TABLE turn_journal (turn_id TEXT, thread_id TEXT, seq INTEGER, message_json TEXT,
            status TEXT, created_at TIMESTAMP);
        CREATE TABLE tool_results (turn_id TEXT, tool_call_id TEXT, content TEXT, created_at TIMESTAMP);
        """
    )
    conn.execute(
        "INSERT INTO active_turns (turn_id, thread_id, status, input_message, started_at) VALUES (?, ?, ?, ?, ?)",
        ("turn-cli", "chat-9", "completed", "покажи статус", "2026-07-01 09:00:00"),
    )
    conn.execute(
        "INSERT INTO turn_journal VALUES (?, ?, ?, ?, 'appended', ?)",
        ("turn-cli", "chat-9", 1, json.dumps({"type": "AIMessage", "content": "всё ок"}), "2026-07-01 09:00:01"),
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_eval_turns_lists_and_suggests_capture(journal, capsys):
    code = main(["eval", "turns", "--limit", "5"])
    out = capsys.readouterr().out

    assert code == 0
    assert "turn-cli" in out
    assert "kaos eval capture --turn turn-cli" in out


def test_eval_capture_writes_a_draft_scenario(journal, tmp_path, capsys):
    suite = tmp_path / "captured"

    code = main(["eval", "capture", "--turn", "turn-cli", "--suite", str(suite), "--name", "status-check"])
    out = capsys.readouterr().out

    assert code == 0
    assert "DRAFT" in out
    scenario = yaml.safe_load((suite / "status-check" / "scenario.yaml").read_text(encoding="utf-8"))
    assert scenario["draft"] is True
    assert scenario["input"] == "покажи статус"


def test_eval_capture_of_unknown_turn_fails_cleanly(journal, tmp_path, capsys):
    code = main(["eval", "capture", "--turn", "missing", "--suite", str(tmp_path / "s")])

    assert code == 1
    assert "Capture failed:" in capsys.readouterr().out


def test_doctor_mentions_evals(tmp_path, capsys):
    from kronos.cli import run_doctor

    run_doctor()

    out = capsys.readouterr().out
    assert "Agent CI" in out
