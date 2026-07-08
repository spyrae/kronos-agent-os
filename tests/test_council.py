"""Council — structured multi-agent debate (roadmap 5.2)."""

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

    # Pin profiles so other modules' global mutation can't leak in.
    from kronos.group_router import AGENT_PROFILES

    original = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({
        "kronos": {"username": "kronosagnt", "aliases": ["kronos"], "role": "strategist"},
        "nexus": {"username": "nexusagnt", "aliases": ["nexus"], "role": "analyst"},
        "lacuna": {"username": "lacunaagnt", "aliases": ["lacuna"], "role": "creative"},
    })

    from kronos import db as _db

    _db._instances.clear()
    import kronos.swarm_store as ss

    ss._singleton = None
    from kronos.swarm_store import get_swarm

    yield get_swarm()

    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(original)


def _council(swarm, initiator="kronos", participants=("nexus", "lacuna"), chat=1):
    return swarm.create_council(
        chat_id=chat, topic_id=None, thread_id=str(chat), initiator=initiator,
        question="what to build next?", participants=list(participants),
    )


def test_participant_sees_task_initiator_and_outsider_do_not(swarm):
    _council(swarm, participants=("nexus", "lacuna"))
    assert len(swarm.pending_council_tasks("nexus")) == 1
    assert len(swarm.pending_council_tasks("lacuna")) == 1
    assert swarm.pending_council_tasks("kronos") == []  # initiator, not a participant
    assert swarm.pending_council_tasks("resonant") == []  # not invited


def test_submit_position_removes_from_pending(swarm):
    sid = _council(swarm, participants=("nexus", "lacuna"))
    swarm.submit_position(sid, "nexus", "focus on metrics")
    assert swarm.pending_council_tasks("nexus") == []
    assert len(swarm.pending_council_tasks("lacuna")) == 1


def test_synthesis_claimed_only_when_all_positions_in(swarm):
    sid = _council(swarm, participants=("nexus", "lacuna"))
    swarm.submit_position(sid, "nexus", "A")
    assert swarm.claim_synthesis(sid, "kronos") is None  # lacuna still missing

    swarm.submit_position(sid, "lacuna", "B")
    claimed = swarm.claim_synthesis(sid, "kronos")
    assert claimed is not None
    # A second poll must not synthesize again.
    assert swarm.claim_synthesis(sid, "kronos") is None
    assert {p["agent_name"] for p in swarm.get_positions(sid)} == {"nexus", "lacuna"}


def test_convene_tool_creates_session(swarm):
    from kronos.tools.council import convene_council

    token = set_tool_audit_context(agent="kronos", thread_id="9:2", session_id="9")
    try:
        result = convene_council.invoke(
            {"question": "strategy?", "participants": ["nexus", "lacuna"]}
        )
    finally:
        reset_tool_audit_context(token)
    assert "консилиум" in result.lower()
    tasks = swarm.pending_council_tasks("nexus")
    assert len(tasks) == 1
    assert tasks[0]["chat_id"] == 9
    assert tasks[0]["topic_id"] == 2


def test_convene_tool_rejects_too_few_participants(swarm):
    from kronos.tools.council import convene_council

    token = set_tool_audit_context(agent="kronos", thread_id="9", session_id="9")
    try:
        result = convene_council.invoke({"question": "q", "participants": ["nexus"]})
    finally:
        reset_tool_audit_context(token)
    assert "минимум 2" in result


def test_convene_tool_drops_self_from_participants(swarm):
    from kronos.tools.council import convene_council

    token = set_tool_audit_context(agent="kronos", thread_id="9", session_id="9")
    try:
        # kronos (self) is filtered out → only nexus left → too few
        result = convene_council.invoke(
            {"question": "q", "participants": ["kronos", "nexus"]}
        )
    finally:
        reset_tool_audit_context(token)
    assert "минимум 2" in result


async def test_intake_participant_submits_position(swarm, monkeypatch):
    from kronos.cron import council as cron_council

    monkeypatch.setattr(settings, "agent_name", "nexus")  # this process = nexus
    sid = _council(swarm, initiator="kronos", participants=("nexus", "lacuna"), chat=5)

    class FakeAgent:
        async def ainvoke(self, message, **kwargs):
            return "nexus position"

    monkeypatch.setattr("kronos.bridge._agent", FakeAgent())

    await cron_council.run_council_intake()

    positions = swarm.get_positions(sid)
    assert any(
        p["agent_name"] == "nexus" and p["position"] == "nexus position" for p in positions
    )


async def test_intake_initiator_synthesizes_when_ready(swarm, monkeypatch):
    from kronos.cron import council as cron_council

    monkeypatch.setattr(settings, "agent_name", "kronos")  # this process = initiator
    sid = _council(swarm, initiator="kronos", participants=("nexus", "lacuna"), chat=5)
    swarm.submit_position(sid, "nexus", "A")
    swarm.submit_position(sid, "lacuna", "B")

    class FakeAgent:
        async def ainvoke(self, message, **kwargs):
            return f"synthesis of: {message[:20]}"

    monkeypatch.setattr("kronos.bridge._agent", FakeAgent())
    sent: list[str] = []
    monkeypatch.setattr(
        cron_council, "send_webhook", lambda text, *a, **k: sent.append(text) or True
    )

    await cron_council.run_council_intake()

    assert sent and sent[0].startswith("synthesis of:")
    assert swarm.councils_awaiting_synthesis("kronos") == []  # session done


async def test_intake_initiator_waits_until_all_positions_in(swarm, monkeypatch):
    from kronos.cron import council as cron_council

    monkeypatch.setattr(settings, "agent_name", "kronos")
    _council(swarm, initiator="kronos", participants=("nexus", "lacuna"), chat=5)
    # only one of two positions submitted

    class FakeAgent:
        async def ainvoke(self, message, **kwargs):
            return "x"

    monkeypatch.setattr("kronos.bridge._agent", FakeAgent())
    swarm.submit_position(swarm.councils_awaiting_synthesis("kronos")[0]["id"], "nexus", "A")
    sent: list[int] = []
    monkeypatch.setattr(cron_council, "send_webhook", lambda *a, **k: sent.append(1) or True)

    await cron_council.run_council_intake()

    assert sent == []  # nothing synthesized yet
    assert len(swarm.councils_awaiting_synthesis("kronos")) == 1  # still gathering
