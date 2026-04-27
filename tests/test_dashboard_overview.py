import json
import sqlite3
from types import SimpleNamespace

import pytest

from dashboard.api import overview


def _configure_paths(tmp_path, monkeypatch):
    db_dir = tmp_path / "kaos"
    monkeypatch.setattr(overview.settings, "agent_name", "kaos")
    monkeypatch.setattr(overview.settings, "workspace_path", str(tmp_path / "workspace"))
    monkeypatch.setattr(overview.settings, "db_dir", str(db_dir))
    monkeypatch.setattr(overview.settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(overview.settings, "mem0_qdrant_path", str(db_dir / "qdrant"))
    monkeypatch.setattr(overview.settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(overview, "_get_scheduler", lambda: None)
    return db_dir


@pytest.mark.asyncio
async def test_control_room_uses_safe_empty_defaults(tmp_path, monkeypatch):
    db_dir = _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(overview.settings, "enable_dynamic_tools", False)
    monkeypatch.setattr(overview.settings, "require_dynamic_tool_sandbox", True)
    monkeypatch.setattr(overview.settings, "enable_mcp_gateway_management", False)
    monkeypatch.setattr(overview.settings, "enable_dynamic_mcp_servers", False)
    monkeypatch.setattr(overview.settings, "enable_server_ops", False)

    result = await overview.get_control_room()

    assert result["runtime"]["agent"] == "kaos"
    assert result["runtime"]["db_dir"] == str(db_dir)
    assert result["safety"]["posture"] == "strict"
    assert result["approvals"]["pending"] == 0
    assert result["jobs"] == {"enabled": 0, "running": 0, "total": 0, "items": []}
    assert result["memory"]["status"] == "not_initialized"
    assert result["coordination"]["status"] == "not_initialized"


@pytest.mark.asyncio
async def test_control_room_aggregates_runtime_state(tmp_path, monkeypatch):
    db_dir = _configure_paths(tmp_path, monkeypatch)
    logs_dir = db_dir / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "audit.jsonl").write_text(
        "\n".join([
            json.dumps({
                "ts": "2026-04-27T10:00:00+0000",
                "session_id": "s1",
                "tier": "standard",
                "duration_ms": 120,
                "approx_cost_usd": 0.0001,
                "input_preview": "search market signal",
            }),
            json.dumps({
                "ts": "2026-04-27T10:03:00+0000",
                "session_id": "s1",
                "tier": "standard",
                "duration_ms": 80,
                "approx_cost_usd": 0.0002,
                "input_preview": "write summary",
            }),
        ]),
        encoding="utf-8",
    )
    (db_dir / "agent_registry.json").write_text(
        json.dumps({"operator": {"enabled": True}, "analyst": {"enabled": False}}),
        encoding="utf-8",
    )

    with sqlite3.connect(db_dir / "memory_fts.db") as conn:
        conn.execute("CREATE TABLE memory_facts (id INTEGER PRIMARY KEY, content TEXT)")
        conn.executemany("INSERT INTO memory_facts (content) VALUES (?)", [("one",), ("two",)])
    with sqlite3.connect(tmp_path / "swarm.db") as conn:
        conn.execute("CREATE TABLE swarm_messages (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reply_claims (state TEXT)")
        conn.execute("CREATE TABLE shared_user_facts (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE swarm_metrics (metric TEXT PRIMARY KEY, value INTEGER)")
        conn.execute("INSERT INTO swarm_messages DEFAULT VALUES")
        conn.execute("INSERT INTO reply_claims VALUES ('claimed')")
        conn.execute("INSERT INTO shared_user_facts DEFAULT VALUES")
        conn.execute("INSERT INTO swarm_metrics VALUES ('duplicate_replies_avoided', 3)")

    fake_scheduler = SimpleNamespace(jobs={
        "heartbeat": SimpleNamespace(
            enabled=True,
            _running=True,
            interval_seconds=300,
            cron_hour=None,
            cron_weekday=None,
            last_run=1777284000.0,
        )
    })
    monkeypatch.setattr(overview, "_get_scheduler", lambda: fake_scheduler)

    result = await overview.get_control_room()

    assert result["agents"] == {"enabled": 1, "total": 2, "primary": "kaos"}
    assert result["runtime"]["audit_entries"] == 2
    assert result["sessions"][0]["id"] == "s1"
    assert result["sessions"][0]["requests"] == 2
    assert [event["type"] for event in result["recent_activity"]] == ["WRITE", "SEARCH"]
    assert result["memory"]["fts_facts"] == 2
    assert result["coordination"]["active_claims"] == 1
    assert result["coordination"]["duplicate_replies_avoided"] == 3
    assert result["jobs"]["running"] == 1
    assert result["jobs"]["items"][0]["schedule"] == "every 5m"
