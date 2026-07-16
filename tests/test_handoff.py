"""Cross-agent hand-off (roadmap 5.1)."""

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

    # Pin agent profiles: other test modules (test_group_router) replace the
    # global AGENT_PROFILES at import time, so we install a known set and
    # restore it, keeping this test immune to that leak.
    from kronos.group_router import AGENT_PROFILES

    original_profiles = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(
        {
            "kronos": {"username": "kronosagnt", "aliases": ["kronos"], "role": "strategic advisor"},
            "nexus": {"username": "nexusagnt", "aliases": ["nexus"], "role": "data analyst"},
            "lacuna": {"username": "lacunaagnt", "aliases": ["lacuna"], "role": "creative director"},
        }
    )

    from kronos import db as _db

    _db._instances.clear()
    import kronos.swarm_store as ss

    ss._singleton = None
    from kronos.swarm_store import get_swarm

    yield get_swarm()

    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(original_profiles)


def _create(swarm, to_agent="nexus", context="analyze metrics", chat=1):
    return swarm.create_handoff(
        chat_id=chat,
        topic_id=None,
        thread_id=str(chat),
        from_agent="kronos",
        to_agent=to_agent,
        context=context,
    )


def test_accept_is_atomic_single_delivery(swarm):
    _create(swarm)
    assert len(swarm.pending_handoffs("nexus")) == 1

    accepted = swarm.accept_next_handoff("nexus")
    assert accepted is not None
    assert accepted["context"] == "analyze metrics"
    # A second poll must not re-claim the same row.
    assert swarm.accept_next_handoff("nexus") is None
    assert swarm.pending_handoffs("nexus") == []


def test_handoff_scoped_to_target_agent(swarm):
    _create(swarm, to_agent="nexus")
    assert swarm.accept_next_handoff("lacuna") is None  # not addressed to lacuna


def test_complete_handoff_clears_pending(swarm):
    hid = _create(swarm)
    swarm.accept_next_handoff("nexus")
    swarm.complete_handoff(hid, success=True)
    assert swarm.pending_handoffs("nexus") == []


def test_tool_creates_handoff_with_chat_and_topic(swarm):
    from kronos.tools.handoff import handoff_to_agent

    token = set_tool_audit_context(agent="kronos", thread_id="55:3", session_id="55")
    try:
        result = handoff_to_agent.invoke({"to_agent": "nexus", "why": "analyze churn"})
    finally:
        reset_tool_audit_context(token)

    assert "Передал" in result and "nexus" in result
    pending = swarm.pending_handoffs("nexus")
    assert len(pending) == 1
    assert pending[0]["chat_id"] == 55
    assert pending[0]["topic_id"] == 3
    assert pending[0]["from_agent"] == "kronos"
    assert pending[0]["context"] == "analyze churn"


def test_tool_rejects_unknown_agent(swarm):
    from kronos.tools.handoff import handoff_to_agent

    token = set_tool_audit_context(agent="kronos", thread_id="55", session_id="55")
    try:
        result = handoff_to_agent.invoke({"to_agent": "ghost", "why": "x"})
    finally:
        reset_tool_audit_context(token)
    assert "неизвестн" in result.lower()
    assert swarm.pending_handoffs("ghost") == []


def test_tool_rejects_self_handoff(swarm):
    from kronos.tools.handoff import handoff_to_agent

    token = set_tool_audit_context(agent="kronos", thread_id="55", session_id="55")
    try:
        result = handoff_to_agent.invoke({"to_agent": "kronos", "why": "x"})
    finally:
        reset_tool_audit_context(token)
    assert "самому себе" in result.lower()


async def test_intake_runs_agent_and_delivers(swarm, monkeypatch):
    from kronos.cron import handoff as cron_handoff

    monkeypatch.setattr(settings, "agent_name", "nexus")  # this process is nexus
    _create(swarm, to_agent="nexus", context="analyze", chat=7)

    class FakeAgent:
        async def ainvoke(self, message, **kwargs):
            return f"analysis: {message}"

    monkeypatch.setattr("kronos.bridge._agent", FakeAgent())
    sent: list[str] = []
    monkeypatch.setattr(cron_handoff, "send_webhook", lambda text, *a, **k: sent.append(text) or True)

    await cron_handoff.run_handoff_intake()

    assert sent and sent[0].startswith("analysis:")
    assert swarm.pending_handoffs("nexus") == []  # completed


async def test_intake_leaves_pending_when_agent_not_ready(swarm, monkeypatch):
    from kronos.cron import handoff as cron_handoff

    monkeypatch.setattr(settings, "agent_name", "nexus")
    _create(swarm, to_agent="nexus", chat=7)
    monkeypatch.setattr("kronos.bridge._agent", None)

    await cron_handoff.run_handoff_intake()

    assert len(swarm.pending_handoffs("nexus")) == 1  # untouched, retried next poll
