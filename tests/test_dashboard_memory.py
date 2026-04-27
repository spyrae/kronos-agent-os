import json
import sqlite3

import pytest

from dashboard.api import memory


def _configure_memory_paths(tmp_path, monkeypatch):
    db_dir = tmp_path / "kaos"
    db_dir.mkdir()
    monkeypatch.setattr(memory.settings, "agent_name", "kaos")
    monkeypatch.setattr(memory.settings, "db_dir", str(db_dir))
    monkeypatch.setattr(memory.settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(memory.settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(memory.settings, "mem0_qdrant_path", str(db_dir / "qdrant"))
    return db_dir


def _seed_memory_dbs(db_dir, swarm_db):
    with sqlite3.connect(db_dir / "memory_fts.db") as conn:
        conn.execute("""
            CREATE TABLE memory_facts (
                id INTEGER PRIMARY KEY,
                user_id TEXT,
                content TEXT,
                source TEXT,
                created_at TEXT,
                mem0_id TEXT,
                relevance REAL,
                tier TEXT,
                last_accessed TEXT
            )
        """)
        conn.execute("CREATE TABLE memory_fts (rowid INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("""
            INSERT INTO memory_facts
            VALUES (1, 'u1', 'User prefers Python examples', 'mem0', '2026-04-27T10:00:00+00:00', 'm1', 0.9, 'active', '2026-04-27T11:00:00+00:00')
        """)
        conn.execute("INSERT INTO memory_fts VALUES (1, 'User prefers Python examples')")

    with sqlite3.connect(db_dir / "knowledge_graph.db") as conn:
        conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, type TEXT, properties TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, relation_type TEXT, properties TEXT, created_at TEXT)")
        conn.execute("INSERT INTO entities VALUES (1, 'KAOS', 'project', '{}', '2026-04-27T10:00:00+00:00', '2026-04-27T10:00:00+00:00')")

    with sqlite3.connect(swarm_db) as conn:
        conn.execute("CREATE TABLE shared_user_facts (id INTEGER PRIMARY KEY, user_id TEXT, fact TEXT, source_agent TEXT, created_at REAL, last_accessed_at REAL, access_count INTEGER)")
        conn.execute("INSERT INTO shared_user_facts VALUES (1, 'u1', 'Shared launch preference', 'kaos', 100.0, 200.0, 2)")

    with sqlite3.connect(db_dir / "session.db") as conn:
        conn.execute("CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, messages TEXT, updated_at TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            ("thread-1", json.dumps([{"type": "HumanMessage", "content": "remember Python"}]), "2026-04-27T12:00:00+00:00"),
        )


@pytest.mark.asyncio
async def test_memory_records_filter_and_delete(tmp_path, monkeypatch):
    db_dir = _configure_memory_paths(tmp_path, monkeypatch)
    _seed_memory_dbs(db_dir, tmp_path / "swarm.db")

    result = await memory.list_memory_records(
        query="python",
        type="all",
        source="all",
        session="all",
        limit=200,
    )

    assert result["total"] == 2
    assert {item["type"] for item in result["records"]} == {"fact", "session"}
    fact = next(item for item in result["records"] if item["type"] == "fact")
    assert fact["metadata"]["tier"] == "active"
    assert "FTS exact recall" in fact["recall_reason"]

    deleted = await memory.delete_memory_record("fts:1")
    result = await memory.list_memory_records(query="", type="fact", source="all", session="all", limit=200)

    assert deleted["ok"] is True
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_memory_reset_requires_confirmation_and_resets_scope(tmp_path, monkeypatch):
    db_dir = _configure_memory_paths(tmp_path, monkeypatch)
    _seed_memory_dbs(db_dir, tmp_path / "swarm.db")

    with pytest.raises(Exception):
        await memory.reset_memory(memory.ResetMemory(scope="sessions", confirm=False))

    reset = await memory.reset_memory(memory.ResetMemory(scope="sessions", confirm=True))
    result = await memory.list_memory_records(query="", type="session", source="all", session="all", limit=200)

    assert reset["ok"] is True
    assert reset["scope"] == "sessions"
    assert result["total"] == 0
