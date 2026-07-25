"""Capturing golden scenarios from the durable turn journal (moat phase 8.3)."""

import json
import sqlite3

import pytest

from kronos.config import settings
from kronos.evals import Scenario, ScenarioError, capture_thread, capture_turn, discover, list_turns


def _seed_journal(db_path, *, turn_id="turn-1", thread_id="chat-1", input_message="покажи расходы за день"):
    """Write a realistic turn: two tool calls, then a final answer."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS active_turns (
            turn_id TEXT PRIMARY KEY, thread_id TEXT, status TEXT,
            input_message TEXT, started_at TIMESTAMP, completed_at TIMESTAMP, error TEXT
        );
        CREATE TABLE IF NOT EXISTS turn_journal (
            turn_id TEXT, thread_id TEXT, seq INTEGER, message_json TEXT,
            status TEXT, created_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tool_results (
            turn_id TEXT, tool_call_id TEXT, content TEXT, created_at TIMESTAMP
        );
        """
    )
    conn.execute(
        "INSERT INTO active_turns (turn_id, thread_id, status, input_message, started_at) VALUES (?, ?, ?, ?, ?)",
        (turn_id, thread_id, "completed", input_message, "2026-07-01 10:00:00"),
    )
    messages = [
        {
            "type": "AIMessage",
            "content": "",
            "tool_calls": [{"name": "get_expenses", "args": {"scope": "day"}, "id": "call_a"}],
        },
        {"type": "ToolMessage", "content": "12.5 USD за кофе", "tool_call_id": "call_a"},
        {
            "type": "AIMessage",
            "content": "",
            "tool_calls": [{"name": "query_notion", "args": {"db": "expenses"}, "id": "call_b"}],
        },
        {"type": "AIMessage", "content": "За день 12.5 USD: кофе."},
    ]
    for seq, payload in enumerate(messages, start=1):
        conn.execute(
            "INSERT INTO turn_journal (turn_id, thread_id, seq, message_json, status) VALUES (?, ?, ?, ?, 'appended')",
            (turn_id, thread_id, seq, json.dumps(payload, ensure_ascii=False)),
        )
    # call_b never produced a ToolMessage (turn interrupted) but was memoized.
    conn.execute(
        "INSERT INTO tool_results (turn_id, tool_call_id, content) VALUES (?, ?, ?)",
        (turn_id, "call_b", "строка из Notion"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def journal(tmp_path, monkeypatch):
    db_dir = tmp_path / "data" / "cap"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "session.db"
    monkeypatch.setattr(settings, "agent_name", "cap")
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_path))
    _seed_journal(db_path)
    return tmp_path


def test_list_turns_returns_recent_first(journal):
    _seed_journal(settings.db_path, turn_id="turn-2", input_message="второй запрос")

    turns = list_turns(limit=10)

    assert {turn["turn_id"] for turn in turns} == {"turn-1", "turn-2"}
    assert list_turns(thread_id="nope") == []


def test_capture_builds_script_from_model_turns(journal, tmp_path):
    scenario = capture_turn("turn-1", suite_dir=tmp_path / "suite")

    assert scenario.input == "покажи расходы за день"
    assert scenario.tool_names == ["get_expenses", "query_notion"]
    assert scenario.script[-1]["content"] == "За день 12.5 USD: кофе."
    assert scenario.source_turn == "turn-1"
    assert scenario.draft is True


def test_capture_collects_tool_outputs_from_both_sources(journal, tmp_path):
    """ToolMessage for the answered call, tool_results for the interrupted one."""
    scenario = capture_turn("turn-1", suite_dir=tmp_path / "suite")

    assert scenario.tool_outputs["get_expenses"] == ["12.5 USD за кофе"]
    assert scenario.tool_outputs["query_notion"] == ["строка из Notion"]


def test_draft_expectations_are_conservative(journal, tmp_path):
    scenario = capture_turn("turn-1", suite_dir=tmp_path / "suite")

    assert scenario.expect.tools_called == ["get_expenses", "query_notion"]
    assert scenario.expect.max_tool_calls == 4  # 2 observed + slack
    # Content assertions are NOT generated: one run's wording is not a spec.
    assert scenario.expect.must_mention == []
    assert scenario.expect.tools_forbidden == []


def test_captured_scenario_round_trips_through_yaml(journal, tmp_path):
    captured = capture_turn("turn-1", suite_dir=tmp_path / "suite", name="expenses-day")

    reloaded = Scenario.load(captured.path)

    assert reloaded.name == "expenses-day"
    assert reloaded.script == captured.script
    assert reloaded.tool_outputs == captured.tool_outputs
    assert reloaded.expect.to_dict() == captured.expect.to_dict()


def test_discover_finds_scenarios_in_a_suite(journal, tmp_path):
    suite = tmp_path / "suite"
    capture_turn("turn-1", suite_dir=suite, name="b-scenario")
    _seed_journal(settings.db_path, turn_id="turn-2", input_message="ещё запрос")
    capture_turn("turn-2", suite_dir=suite, name="a-scenario")

    found = discover(suite)

    assert [scenario.name for scenario in found] == ["a-scenario", "b-scenario"]
    assert discover(tmp_path / "missing") == []


def test_capture_refuses_content_with_personal_data(journal, tmp_path):
    _seed_journal(
        settings.db_path,
        turn_id="turn-pii",
        input_message="напиши на почту",
    )
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "INSERT INTO turn_journal (turn_id, thread_id, seq, message_json, status) VALUES (?, ?, ?, ?, 'appended')",
        (
            "turn-pii",
            "chat-1",
            9,
            json.dumps({"type": "AIMessage", "content": "звони +7 916 123 45 67"}, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    # redact_private_text masks the number, so the guard passes and the phone is gone.
    scenario = capture_turn("turn-pii", suite_dir=tmp_path / "suite")
    blob = json.dumps(scenario.to_dict(), ensure_ascii=False)

    assert "916 123 45 67" not in blob
    assert "***" in blob


def test_capture_of_unknown_turn_is_explicit(journal, tmp_path):
    with pytest.raises(ScenarioError, match="turn not found"):
        capture_turn("nope", suite_dir=tmp_path / "suite")


def test_capture_of_turn_without_model_answers_is_skipped(journal, tmp_path):
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "INSERT INTO active_turns (turn_id, thread_id, status, input_message, started_at) VALUES (?, ?, ?, ?, ?)",
        ("turn-empty", "chat-1", "failed", "ничего не вышло", "2026-07-01 11:00:00"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ScenarioError, match="no journalled messages"):
        capture_turn("turn-empty", suite_dir=tmp_path / "suite")


def test_capture_thread_reports_skips_without_failing(journal, tmp_path):
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "INSERT INTO active_turns (turn_id, thread_id, status, input_message, started_at) VALUES (?, ?, ?, ?, ?)",
        ("turn-empty", "chat-1", "failed", "пусто", "2026-07-01 12:00:00"),
    )
    conn.commit()
    conn.close()

    report = capture_thread("chat-1", suite_dir=tmp_path / "suite", last=5)

    assert len(report.scenarios) == 1
    assert any("no journalled messages" in reason for reason in report.skipped)
    assert "DRAFT" in report.render()


def test_scenario_without_script_is_rejected(tmp_path):
    path = tmp_path / "broken"
    path.mkdir()
    (path / "scenario.yaml").write_text("name: broken\ninput: hi\n", encoding="utf-8")

    with pytest.raises(ScenarioError, match="no script"):
        Scenario.load(path)


def test_scenario_from_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future"
    path.mkdir()
    (path / "scenario.yaml").write_text(
        "schema_version: 99\nname: future\ninput: hi\nscript:\n  - content: hi\n", encoding="utf-8"
    )

    with pytest.raises(ScenarioError, match="newer than supported"):
        Scenario.load(path)
