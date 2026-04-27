"""Deterministic demo state for launch screenshots and local demos."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _init_session_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (thread_id TEXT PRIMARY KEY, messages TEXT NOT NULL DEFAULT '[]', updated_at TEXT)")
        messages = [
            {"type": "HumanMessage", "content": "Plan the KAOS open-source launch."},
            {"type": "AIMessage", "content": "I will split it into docs, dashboard, Docker quickstart, templates, and launch assets."},
        ]
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
            ("demo-launch", json.dumps(messages, ensure_ascii=False), "2026-04-27T09:12:00+00:00"),
        )


def _init_memory(db_dir: Path) -> None:
    with sqlite3.connect(db_dir / "memory_fts.db") as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT DEFAULT 'demo',
                created_at TEXT NOT NULL,
                mem0_id TEXT,
                relevance REAL DEFAULT 1.0,
                tier TEXT DEFAULT 'active',
                last_accessed TEXT
            );
            CREATE TABLE IF NOT EXISTS memory_fts (rowid INTEGER PRIMARY KEY, content TEXT, user_id TEXT);
            DELETE FROM memory_fts;
            DELETE FROM memory_facts;
        """)
        facts = [
            "Launch reviewers prefer concise technical answers.",
            "Demo screenshots must not include private Telegram IDs or live memory.",
            "KAOS should be framed as Agent OS with optional swarm coordination.",
        ]
        for fact in facts:
            cursor = conn.execute(
                "INSERT INTO memory_facts (user_id, content, source, created_at, last_accessed, relevance, tier) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("demo-user", fact, "demo", "2026-04-27T09:00:00+00:00", "2026-04-27T09:10:00+00:00", 0.94, "active"),
            )
            conn.execute("INSERT INTO memory_fts (rowid, content, user_id) VALUES (?, ?, ?)", (cursor.lastrowid, fact, "demo-user"))

    with sqlite3.connect(db_dir / "knowledge_graph.db") as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                properties TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                properties TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            DELETE FROM relations;
            DELETE FROM entities;
        """)
        kaos = conn.execute("INSERT INTO entities (name, type, created_at, updated_at) VALUES ('KAOS', 'project', '2026-04-27T09:00:00+00:00', '2026-04-27T09:00:00+00:00')").lastrowid
        dashboard = conn.execute("INSERT INTO entities (name, type, created_at, updated_at) VALUES ('Control Room', 'tool', '2026-04-27T09:00:00+00:00', '2026-04-27T09:00:00+00:00')").lastrowid
        conn.execute("INSERT INTO relations (source_id, target_id, relation_type, created_at) VALUES (?, ?, 'uses', '2026-04-27T09:00:00+00:00')", (kaos, dashboard))


def _init_swarm(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS swarm_messages (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL DEFAULT 0,
                msg_id INTEGER NOT NULL,
                reply_to_msg_id INTEGER,
                sender_id INTEGER NOT NULL DEFAULT 0,
                sender_type TEXT NOT NULL,
                agent_name TEXT,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, topic_id, msg_id)
            );
            CREATE TABLE IF NOT EXISTS reply_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL DEFAULT 0,
                root_msg_id INTEGER NOT NULL,
                trigger_msg_id INTEGER NOT NULL,
                agent_name TEXT NOT NULL,
                tier INTEGER NOT NULL,
                eta_ts REAL NOT NULL,
                state TEXT NOT NULL,
                reason TEXT,
                reply_msg_id INTEGER,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS swarm_metrics (metric TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS shared_user_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                fact TEXT NOT NULL,
                source_agent TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0
            );
            DELETE FROM swarm_messages;
            DELETE FROM reply_claims;
            DELETE FROM swarm_metrics;
            DELETE FROM shared_user_facts;
        """)
        conn.execute("INSERT INTO swarm_messages VALUES (100, 0, 1, NULL, 42, 'user', NULL, 'Plan KAOS launch.', 1777280400)")
        claims = [
            ("researcher", 2, 1777280405, "Find comparable open-source launch patterns.", "sent", 11),
            ("critic", 2, 1777280410, "Identify setup and trust risks.", "sent", 12),
            ("operator", 2, 1777280415, "Convert recommendations into tasks.", "sent", 13),
            ("synthesizer", 1, 1777280420, "Merge into one final plan.", "sent", 14),
        ]
        for idx, (agent, tier, eta, reason, state, reply_id) in enumerate(claims, start=1):
            conn.execute(
                "INSERT INTO reply_claims (chat_id, topic_id, root_msg_id, trigger_msg_id, agent_name, tier, eta_ts, state, reason, reply_msg_id, created_at) VALUES (?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (100, idx, agent, tier, eta, state, reason, reply_id, eta),
            )
            conn.execute("INSERT INTO swarm_messages VALUES (100, 0, ?, 1, -1, 'agent', ?, ?, ?)", (reply_id, agent, reason, eta + 5))
        conn.execute("INSERT INTO swarm_messages VALUES (100, 0, 20, 1, -1, 'agent', 'synthesizer', 'Final launch plan: quickstart, dashboard visual, templates, trust docs, and demo seed.', 1777280450)")
        conn.execute("INSERT INTO swarm_metrics VALUES ('duplicate_replies_avoided', 3, 1777280450)")
        conn.execute("INSERT INTO shared_user_facts (user_id, fact, source_agent, created_at, last_accessed_at, access_count) VALUES ('demo-user', 'User wants KAOS positioned as Agent OS, not council-only.', 'synthesizer', 1777280450, 1777280450, 1)")


def seed_demo_state(data_dir: Path, workspace_dir: Path, swarm_db: Path, *, reset: bool = False) -> dict:
    data_dir = data_dir.expanduser().resolve()
    workspace_dir = workspace_dir.expanduser().resolve()
    swarm_db = swarm_db.expanduser().resolve()

    if reset and data_dir.exists():
        if "demo" not in data_dir.name.lower():
            raise ValueError(f"Refusing to reset non-demo data dir: {data_dir}")
        shutil.rmtree(data_dir)
    if reset and workspace_dir.exists() and "demo" in workspace_dir.name.lower():
        shutil.rmtree(workspace_dir)

    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = data_dir / "logs"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    _init_session_db(data_dir / "session.db")
    _init_memory(data_dir)
    _init_swarm(swarm_db)

    _write_jsonl(logs_dir / "audit.jsonl", [
        {"ts": "2026-04-27T09:01:00+0000", "user_id": "demo-user", "session_id": "demo-launch", "tier": "standard", "duration_ms": 820, "approx_cost_usd": 0.0004, "input_preview": "Plan KAOS launch", "output_preview": "Split into docs, dashboard, templates, Docker, and launch assets."},
        {"ts": "2026-04-27T09:04:00+0000", "user_id": "demo-user", "session_id": "demo-launch", "tier": "standard", "duration_ms": 640, "approx_cost_usd": 0.0003, "input_preview": "Show dashboard status", "output_preview": "Control room is healthy."},
    ])
    _write_jsonl(logs_dir / "cost.jsonl", [
        {"ts": "2026-04-27T09:01:00+0000", "tier": "standard", "input_tokens": 120, "output_tokens": 340, "cost_usd": 0.0004},
        {"ts": "2026-04-27T09:04:00+0000", "tier": "standard", "input_tokens": 80, "output_tokens": 220, "cost_usd": 0.0003},
    ])
    _write_jsonl(logs_dir / "tool_calls.jsonl", [
        {"ts": "2026-04-27T09:02:00+0000", "event": "tool_call", "status": "called", "tool": "load_skill", "capability": "skills", "approval_status": "not_required", "call_id": "demo-call-1", "agent": "demo", "thread_id": "demo-launch", "session_id": "demo-launch", "args_summary": "{\"skill\":\"research-brief\"}", "result_summary": "", "error": False, "duration_ms": None},
        {"ts": "2026-04-27T09:02:01+0000", "event": "tool_result", "status": "ok", "tool": "load_skill", "capability": "skills", "approval_status": "not_required", "call_id": "demo-call-1", "agent": "demo", "thread_id": "demo-launch", "session_id": "demo-launch", "args_summary": "{}", "result_summary": "Loaded research brief protocol.", "error": False, "duration_ms": 22},
        {"ts": "2026-04-27T09:03:00+0000", "event": "tool_result", "status": "blocked", "tool": "mcp_add_server", "capability": "mcp", "approval_status": "blocked", "call_id": "demo-call-2", "agent": "demo", "thread_id": "demo-launch", "session_id": "demo-launch", "args_summary": "{\"name\":\"demo-server\"}", "result_summary": "Blocked: dynamic MCP server management is disabled.", "error": True, "duration_ms": 4},
    ])
    _write_jsonl(logs_dir / "approval_queue.jsonl", [
        {"event": "created", "id": "apr_demo_server_ops", "kind": "capability_change", "capability": "server_ops", "capability_name": "Server operations", "action": "enable", "status": "pending", "risk": "critical", "scope": "runtime", "owner": str(workspace_dir), "required_env": "ENABLE_SERVER_OPS=true plus private servers.yaml", "reason": "demo incident review", "requested_at": "2026-04-27T09:05:00+00:00", "requested_by": "dashboard", "effect": "no_runtime_change_until_env_restart"},
        {"event": "created", "id": "apr_demo_mcp", "kind": "capability_change", "capability": "mcp_gateway_management", "capability_name": "MCP gateway management", "action": "enable", "status": "pending", "risk": "high", "scope": "runtime", "owner": str(workspace_dir), "required_env": "ENABLE_MCP_GATEWAY_MANAGEMENT=true", "reason": "demo connector setup", "requested_at": "2026-04-27T09:06:00+00:00", "requested_by": "dashboard", "effect": "no_runtime_change_until_env_restart"},
        {"event": "decided", "id": "apr_demo_mcp", "decision": "denied", "reason": "keep demo safe", "decided_at": "2026-04-27T09:07:00+00:00", "decided_by": "dashboard"},
    ])
    _write_jsonl(logs_dir / "cron_runs.jsonl", [
        {"ts": "2026-04-27T09:00:00+00:00", "job": "heartbeat", "status": "ok", "duration_ms": 145, "error": "", "enabled": True, "agent": "demo"},
        {"ts": "2026-04-27T08:00:00+00:00", "job": "demo-daily-brief", "status": "error", "duration_ms": 87, "error": "provider not configured", "enabled": False, "agent": "demo"},
    ])

    skill_dir = workspace_dir / "skills" / "research-brief"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "IDENTITY.md").write_text("# Demo KAOS Agent\n\nSafe public demo workspace.\n", encoding="utf-8")
    (workspace_dir / "SOUL.md").write_text("# Demo Principles\n\nBe concise, auditable, and local-first.\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("# Research Brief\n\nSummarize sources, risks, and next actions without private data.\n", encoding="utf-8")

    return {
        "data_dir": str(data_dir),
        "workspace_dir": str(workspace_dir),
        "swarm_db": str(swarm_db),
        "records": {
            "sessions": 1,
            "facts": 3,
            "tool_events": 3,
            "approvals": 2,
            "swarm_runs": 1,
        },
    }
