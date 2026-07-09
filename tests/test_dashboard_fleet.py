"""Fleet view API — whole-swarm overview from the shared ledger."""

import sqlite3
import time

import pytest

from dashboard.api import fleet


def _seed_ledger(db_path, now: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE swarm_messages (
                chat_id INTEGER, topic_id INTEGER, msg_id INTEGER,
                reply_to_msg_id INTEGER, sender_id INTEGER, sender_type TEXT,
                agent_name TEXT, text TEXT, created_at REAL
            );
            CREATE TABLE reply_claims (
                id INTEGER PRIMARY KEY, chat_id INTEGER, topic_id INTEGER,
                root_msg_id INTEGER, trigger_msg_id INTEGER, agent_name TEXT,
                tier INTEGER, eta_ts REAL, state TEXT, reason TEXT,
                reply_msg_id INTEGER, created_at REAL
            );
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
            CREATE TABLE memory_requests (
                id INTEGER PRIMARY KEY, chat_id INTEGER, topic_id INTEGER,
                thread_id TEXT, from_agent TEXT, to_agent TEXT, query TEXT,
                state TEXT, created_at REAL
            );
            CREATE TABLE shared_user_facts (
                id INTEGER PRIMARY KEY, fact TEXT
            );
            CREATE TABLE swarm_metrics (metric TEXT PRIMARY KEY, value INTEGER);
        """)
        conn.execute(
            "INSERT INTO swarm_messages VALUES (1, 0, 1, NULL, 42, 'user', NULL, 'hi all', ?)",
            (now - 600,),
        )
        conn.execute(
            "INSERT INTO swarm_messages VALUES (1, 0, 2, 1, 100, 'agent', 'kronos', 'on it', ?)",
            (now - 500,),
        )
        conn.execute(
            "INSERT INTO swarm_messages VALUES (1, 0, 3, 1, 101, 'agent', 'nexus', 'numbers say yes', ?)",
            (now - 400,),
        )
        conn.execute(
            "INSERT INTO reply_claims VALUES (1, 1, 0, 1, 1, 'kronos', 1, 0, 'sent', 'explicit @kronos', NULL, ?)",
            (now - 500,),
        )
        conn.execute(
            "INSERT INTO reply_claims VALUES (2, 1, 0, 1, 1, 'nexus', 2, 1, 'cancelled', 'lost arbitration', NULL, ?)",
            (now - 500,),
        )
        conn.execute(
            "INSERT INTO handoffs VALUES (1, 1, 0, '1', 'kronos', 'nexus', 'analytics question', 'pending', ?, NULL)",
            (now - 300,),
        )
        conn.execute(
            "INSERT INTO council_sessions VALUES (1, 1, 0, '1', 'kronos', 'ship it?', 'nexus,lacuna', 'gathering', ?)",
            (now - 200,),
        )
        conn.execute(
            "INSERT INTO memory_requests VALUES (1, 1, 0, '1', 'impulse', 'kronos', 'plans for Q3?', 'pending', ?)",
            (now - 100,),
        )
        conn.execute("INSERT INTO shared_user_facts (fact) VALUES ('likes coffee')")
        conn.execute("INSERT INTO swarm_metrics VALUES ('duplicate_replies_avoided', 7)")


@pytest.mark.asyncio
async def test_fleet_overview_empty_without_db(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet.settings, "swarm_db_path", str(tmp_path / "missing.db"))
    monkeypatch.delenv("FLEET_HEALTH_PORTS", raising=False)

    result = await fleet.fleet_overview()

    # Profiles still listed (from agents.yaml), just with zeroed stats.
    assert result["timeline"] == []
    assert result["totals"]["agent_messages_24h"] == 0
    assert result["health_probes_configured"] is False


@pytest.mark.asyncio
async def test_fleet_overview_aggregates_ledger(tmp_path, monkeypatch):
    db_path = tmp_path / "swarm.db"
    now = time.time()
    _seed_ledger(db_path, now)
    monkeypatch.setattr(fleet.settings, "swarm_db_path", str(db_path))
    monkeypatch.setattr(fleet.settings, "agent_name", "kronos")
    monkeypatch.delenv("FLEET_HEALTH_PORTS", raising=False)

    result = await fleet.fleet_overview()
    agents = {a["name"]: a for a in result["agents"]}

    kronos = agents["kronos"]
    assert kronos["is_me"] is True
    assert kronos["messages_24h"] == 1
    assert kronos["replies_won_24h"] == 1
    assert kronos["last_seen"] == pytest.approx(now - 500, abs=2)
    assert sum(kronos["sparkline"]) == 1

    nexus = agents["nexus"]
    assert nexus["claims_yielded_24h"] == 1
    assert nexus["handoffs_pending"] == 1

    totals = result["totals"]
    assert totals["user_messages_24h"] == 1
    assert totals["agent_messages_24h"] == 2
    assert totals["active_councils"] == 1
    assert totals["pending_handoffs"] == 1
    assert totals["pending_memory_requests"] == 1
    assert totals["shared_facts"] == 1
    assert totals["metrics"]["duplicate_replies_avoided"] == 7

    kinds = [event["kind"] for event in result["timeline"]]
    assert kinds[:4] == ["memory", "council", "handoff", "reply"]  # newest first


@pytest.mark.asyncio
async def test_fleet_health_ports_parsing(monkeypatch):
    monkeypatch.setenv("FLEET_HEALTH_PORTS", "kronos=8788, nexus=8794,bad,noport=")

    assert fleet._health_ports() == {"kronos": 8788, "nexus": 8794}
