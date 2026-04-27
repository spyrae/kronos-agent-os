import json
import sqlite3

import pytest

from kronos.cli import main
from kronos.demo_seed import seed_demo_state


def _count(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_seed_demo_state_creates_full_public_fixture(tmp_path):
    data_dir = tmp_path / "kaos-demo"
    workspace_dir = tmp_path / "workspace-demo"
    result = seed_demo_state(data_dir, workspace_dir, data_dir / "swarm.db", reset=True)

    assert result["records"] == {
        "sessions": 1,
        "facts": 3,
        "tool_events": 3,
        "approvals": 2,
        "swarm_runs": 1,
    }
    assert _count(data_dir / "session.db", "sessions") == 1
    assert _count(data_dir / "memory_fts.db", "memory_facts") == 3
    assert _count(data_dir / "knowledge_graph.db", "entities") == 2
    assert _count(data_dir / "swarm.db", "reply_claims") == 4
    assert _count(data_dir / "swarm.db", "shared_user_facts") == 1

    tool_events = [
        json.loads(line)
        for line in (data_dir / "logs" / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    approvals = [
        json.loads(line)
        for line in (data_dir / "logs" / "approval_queue.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert {event["tool"] for event in tool_events} == {"load_skill", "mcp_add_server"}
    assert any(event["approval_status"] == "blocked" for event in tool_events)
    assert any(event["status"] == "pending" for event in approvals)
    assert (workspace_dir / "IDENTITY.md").is_file()
    assert (workspace_dir / "skills" / "research-brief" / "SKILL.md").is_file()


def test_seed_demo_state_reset_refuses_non_demo_directory(tmp_path):
    data_dir = tmp_path / "production"
    data_dir.mkdir()

    with pytest.raises(ValueError, match="Refusing to reset non-demo"):
        seed_demo_state(data_dir, tmp_path / "workspace-demo", data_dir / "swarm.db", reset=True)


def test_cli_demo_seed_command(tmp_path, capsys):
    data_dir = tmp_path / "dashboard-demo"
    workspace_dir = tmp_path / "agent-demo"

    result = main([
        "demo-seed",
        "--data-dir",
        str(data_dir),
        "--workspace",
        str(workspace_dir),
        "--swarm-db",
        str(data_dir / "swarm.db"),
        "--reset",
    ])

    out = capsys.readouterr().out
    assert result == 0
    assert "KAOS demo state seeded" in out
    assert "kaos dashboard" in out
    assert _count(data_dir / "session.db", "sessions") == 1
