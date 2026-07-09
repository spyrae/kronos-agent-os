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


@pytest.mark.asyncio
async def test_collaboration_empty_without_db(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.settings, "swarm_db_path", str(tmp_path / "missing.db"))

    result = await swarm.swarm_collaboration()

    assert result == {"handoffs": [], "councils": [], "memory_requests": [], "total": 0}


@pytest.mark.asyncio
async def test_collaboration_lists_handoffs_councils_and_memory_requests(tmp_path, monkeypatch):
    db_path = tmp_path / "swarm.db"
    monkeypatch.setattr(swarm.settings, "swarm_db_path", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE handoffs (
                id INTEGER PRIMARY KEY, chat_id INTEGER, topic_id INTEGER,
                thread_id TEXT, from_agent TEXT, to_agent TEXT, context TEXT,
                state TEXT, created_at REAL, accepted_at REAL
            );
            CREATE TABLE council_sessions (
                id INTEGER PRIMARY KEY, chat_id INTEGER, topic_id INTEGER,
                thread_id TEXT, initiator TEXT, question TEXT, participants TEXT,
                state TEXT, created_at REAL
            );
            CREATE TABLE council_positions (
                id INTEGER PRIMARY KEY, session_id INTEGER, agent_name TEXT,
                position TEXT, created_at REAL
            );
            CREATE TABLE memory_requests (
                id INTEGER PRIMARY KEY, chat_id INTEGER, topic_id INTEGER,
                thread_id TEXT, from_agent TEXT, to_agent TEXT, query TEXT,
                state TEXT, created_at REAL
            );
            INSERT INTO handoffs VALUES (1, 10, 0, '10', 'kronos', 'nexus', 'analytics question', 'done', 100.0, 101.0);
            INSERT INTO council_sessions VALUES (7, 10, 0, '10', 'kronos', 'ship or wait?', 'nexus,lacuna', 'done', 100.0);
            INSERT INTO council_positions VALUES (1, 7, 'nexus', 'ship', 100.5);
            INSERT INTO council_positions VALUES (2, 7, 'lacuna', 'wait', 100.6);
            INSERT INTO memory_requests VALUES (3, 10, 0, '10', 'impulse', 'kronos', 'what about X', 'pending', 102.0);
        """)

    result = await swarm.swarm_collaboration()

    assert result["total"] == 3
    assert result["handoffs"][0]["from_agent"] == "kronos"
    assert result["handoffs"][0]["state"] == "done"
    council = result["councils"][0]
    assert council["question"] == "ship or wait?"
    assert [p["agent_name"] for p in council["positions"]] == ["nexus", "lacuna"]
    assert result["memory_requests"][0]["query"] == "what about X"
