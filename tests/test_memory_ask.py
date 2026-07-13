"""Cross-agent memory queries (roadmap 5.3)."""

import pytest

from kronos.audit import reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    swarm_path = tmp_path / "swarm.db"
    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "swarm_db_path", str(swarm_path))
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")

    from kronos.group_router import AGENT_PROFILES

    original = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(
        {
            "kronos": {"username": "kronosagnt", "aliases": ["kronos"], "role": "strategist"},
            "nexus": {"username": "nexusagnt", "aliases": ["nexus"], "role": "analyst"},
            "lacuna": {"username": "lacunaagnt", "aliases": ["lacuna"], "role": "creative"},
        }
    )

    from kronos import db as _db

    _db._instances.clear()
    import kronos.swarm_store as ss

    ss._singleton = None
    from kronos.swarm_store import get_swarm

    yield get_swarm()

    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(original)


def _req(swarm, to_agent="nexus", query="what about X", chat=1):
    return swarm.create_memory_request(
        chat_id=chat,
        topic_id=None,
        thread_id=str(chat),
        from_agent="kronos",
        to_agent=to_agent,
        query=query,
    )


def test_accept_is_atomic_single_answer(swarm):
    _req(swarm)
    assert len(swarm.pending_memory_requests("nexus")) == 1
    accepted = swarm.accept_next_memory_request("nexus")
    assert accepted is not None
    assert accepted["query"] == "what about X"
    assert swarm.accept_next_memory_request("nexus") is None  # not re-claimed
    assert swarm.pending_memory_requests("nexus") == []


def test_scoped_per_target_agent(swarm):
    _req(swarm, to_agent="nexus")
    assert swarm.accept_next_memory_request("lacuna") is None


def test_tool_creates_request_with_chat_and_topic(swarm):
    from kronos.tools.memory_ask import ask_agent_memory

    token = set_tool_audit_context(agent="kronos", thread_id="8:4", session_id="8")
    try:
        result = ask_agent_memory.invoke({"to_agent": "nexus", "query": "past decisions on pricing"})
    finally:
        reset_tool_audit_context(token)

    assert "Спросил" in result and "nexus" in result
    pending = swarm.pending_memory_requests("nexus")
    assert len(pending) == 1
    assert pending[0]["chat_id"] == 8
    assert pending[0]["topic_id"] == 4
    assert pending[0]["query"] == "past decisions on pricing"


def test_tool_rejects_unknown_agent(swarm):
    from kronos.tools.memory_ask import ask_agent_memory

    token = set_tool_audit_context(agent="kronos", thread_id="8", session_id="8")
    try:
        result = ask_agent_memory.invoke({"to_agent": "ghost", "query": "x"})
    finally:
        reset_tool_audit_context(token)
    assert "неизвестн" in result.lower()


def test_tool_rejects_self(swarm):
    from kronos.tools.memory_ask import ask_agent_memory

    token = set_tool_audit_context(agent="kronos", thread_id="8", session_id="8")
    try:
        result = ask_agent_memory.invoke({"to_agent": "kronos", "query": "x"})
    finally:
        reset_tool_audit_context(token)
    assert "свою память" in result.lower()


async def test_intake_answers_from_memory_and_delivers(swarm, monkeypatch):
    from kronos.cron import memory_ask as cron_mem

    monkeypatch.setattr(settings, "agent_name", "nexus")  # this process = nexus
    _req(swarm, to_agent="nexus", query="pricing history", chat=7)

    class FakeAgent:
        async def ainvoke(self, message, **kwargs):
            return f"from memory: {message}"

    monkeypatch.setattr("kronos.bridge._agent", FakeAgent())
    sent: list[str] = []
    monkeypatch.setattr(cron_mem, "send_webhook", lambda text, *a, **k: sent.append(text) or True)

    await cron_mem.run_memory_intake()

    assert sent and sent[0].startswith("from memory:")
    assert swarm.pending_memory_requests("nexus") == []


async def test_intake_leaves_pending_when_agent_not_ready(swarm, monkeypatch):
    from kronos.cron import memory_ask as cron_mem

    monkeypatch.setattr(settings, "agent_name", "nexus")
    _req(swarm, to_agent="nexus", chat=7)
    monkeypatch.setattr("kronos.bridge._agent", None)

    await cron_mem.run_memory_intake()

    assert len(swarm.pending_memory_requests("nexus")) == 1  # untouched, retried
