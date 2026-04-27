import sqlite3

import pytest

from dashboard.api import swarm


@pytest.mark.asyncio
async def test_swarm_runs_returns_demo_without_db(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.settings, "swarm_db_path", str(tmp_path / "missing.db"))

    result = await swarm.list_swarm_runs()

    assert result["demo"] is True
    assert result["runs"][0]["id"] == "demo-launch-plan"
    assert len(result["runs"][0]["roles"]) >= 3


@pytest.mark.asyncio
async def test_swarm_runs_builds_from_claims_and_messages(tmp_path, monkeypatch):
    db_path = tmp_path / "swarm.db"
    monkeypatch.setattr(swarm.settings, "swarm_db_path", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE reply_claims (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                topic_id INTEGER,
                root_msg_id INTEGER,
                trigger_msg_id INTEGER,
                agent_name TEXT,
                tier INTEGER,
                eta_ts REAL,
                state TEXT,
                reason TEXT,
                reply_msg_id INTEGER,
                created_at REAL
            );
            CREATE TABLE swarm_messages (
                msg_id INTEGER,
                reply_to_msg_id INTEGER,
                chat_id INTEGER,
                topic_id INTEGER,
                sender_type TEXT,
                agent_name TEXT,
                text TEXT,
                created_at REAL
            );
            CREATE TABLE swarm_metrics (metric TEXT PRIMARY KEY, value INTEGER);
        """)
        conn.execute("INSERT INTO reply_claims VALUES (1, 10, 0, 100, 101, 'researcher', 2, 10.0, 'claimed', 'research angle', NULL, 1.0)")
        conn.execute("INSERT INTO reply_claims VALUES (2, 10, 0, 100, 102, 'synthesizer', 1, 12.0, 'sent', 'final answer', 201, 2.0)")
        conn.execute("INSERT INTO swarm_messages VALUES (100, NULL, 10, 0, 'user', NULL, 'plan launch', 1.0)")
        conn.execute("INSERT INTO swarm_messages VALUES (201, 100, 10, 0, 'agent', 'synthesizer', 'final synthesis', 3.0)")
        conn.execute("INSERT INTO swarm_metrics VALUES ('duplicate_replies_avoided', 4)")

    result = await swarm.list_swarm_runs()
    run = result["runs"][0]

    assert result["demo"] is False
    assert run["metrics"]["claims"] == 2
    assert run["metrics"]["duplicate_replies_avoided"] == 4
    assert any(role["status"] == "winner" for role in run["roles"])
    assert run["final"] == "final synthesis"
