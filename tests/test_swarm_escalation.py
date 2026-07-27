"""SLA escalation for owned topics (moat phase 11.2).

Ownership only helps if silence has a consequence. These tests pin the two
properties that make escalation safe with six pollers on one ledger: the watch
is a single row per message, and exactly one process turns it into a hand-off.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kronos.config import settings
from kronos.group_router import GroupRouter

MY_ID = 1001
USER_ID = 42
ALLOWED_USERS = {USER_ID}

PROFILES = {
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["kronos"],
        "role": "strategic advisor",
        "owns": ["planning"],
        "escalates_to": "nexus",
        "sla_minutes": 15,
    },
    "nexus": {
        "username": "nexusagnt",
        "aliases": ["nexus"],
        "role": "data analyst",
        "owns": ["metrics"],
    },
    "impulse": {
        "username": "impulseagnt",
        "aliases": ["impulse"],
        "role": "action catalyst",
    },
}


@pytest.fixture(autouse=True)
def registry():
    """Swap the live registry for a swarm with declared ownership."""
    from kronos.group_router import AGENT_PROFILES

    original = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({name: dict(prof) for name, prof in PROFILES.items()})
    yield
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(original)


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    import kronos.db as _db

    _db._instances.clear()
    from kronos.swarm_store import SwarmStore

    store = SwarmStore()
    yield store
    _db._instances.clear()


def _router(agent_name: str) -> GroupRouter:
    return GroupRouter(
        agent_name=agent_name,
        my_id=MY_ID,
        my_username=PROFILES[agent_name]["username"],
        allowed_user_ids=ALLOWED_USERS,
    )


def _event(text: str, *, msg_id: int = 100, topic_label: str | None = None):
    event = MagicMock()
    event.raw_text = text
    event.sender_id = USER_ID
    event.message = MagicMock()
    event.message.id = msg_id
    event.message.entities = []
    event.is_reply = False
    event.get_reply_message = AsyncMock(return_value=None)
    # MagicMock invents attributes; the router reads topic_label with getattr.
    event.topic_label = topic_label
    return event


# --- the watch ledger ---------------------------------------------------------


def _watch(swarm, *, root_msg_id: int = 100, sla_minutes: int = 15, owner: str = "kronos") -> bool:
    return swarm.watch_sla(
        chat_id=-100123,
        topic_id=7,
        root_msg_id=root_msg_id,
        thread_id="-100123:7",
        topic="planning",
        owner_agent=owner,
        request="распланируй квартал",
        sla_minutes=sla_minutes,
    )


def test_only_the_first_agent_registers_the_watch(swarm):
    assert _watch(swarm) is True
    assert _watch(swarm) is False, "six agents see the same message; the watch is one row"
    assert len(swarm.sla_watches()) == 1


def test_a_watch_is_not_due_before_its_deadline(swarm):
    _watch(swarm, sla_minutes=15)

    assert swarm.due_sla_watches() == []
    assert len(swarm.due_sla_watches(now=time.time() + 16 * 60)) == 1


def test_resolving_a_watch_is_a_compare_and_set(swarm):
    _watch(swarm)
    watch_id = swarm.sla_watches()[0]["id"]

    assert swarm.resolve_sla_watch(watch_id, state="escalated") is True
    assert swarm.resolve_sla_watch(watch_id, state="escalated") is False


def test_an_unknown_state_is_rejected(swarm):
    _watch(swarm)

    with pytest.raises(ValueError):
        swarm.resolve_sla_watch(swarm.sla_watches()[0]["id"], state="whatever")


# --- escalation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_creates_exactly_one_handoff(swarm, monkeypatch):
    from kronos.cron.escalation import run_sla_escalation

    monkeypatch.setattr(settings, "agent_name", "impulse")
    _watch(swarm, sla_minutes=1)
    monkeypatch.setattr(
        swarm,
        "due_sla_watches",
        lambda **kwargs: [dict(row) for row in swarm.sla_watches()],
    )
    monkeypatch.setattr("kronos.swarm_store.get_swarm", lambda: swarm)
    monkeypatch.setattr("kronos.cron.escalation.get_swarm", lambda: swarm)

    await run_sla_escalation()
    await run_sla_escalation()  # a second poll (or a second process) must not repeat it

    pending = swarm.pending_handoffs("nexus")
    assert len(pending) == 1
    assert "planning" in pending[0]["context"]
    assert swarm.get_metrics().get("escalations_triggered") == 1
    assert swarm.sla_watches()[0]["state"] == "escalated"


@pytest.mark.asyncio
async def test_an_answered_topic_is_not_escalated(swarm, monkeypatch):
    from kronos.cron.escalation import run_sla_escalation

    monkeypatch.setattr(settings, "agent_name", "impulse")
    _watch(swarm, sla_minutes=1)
    swarm.claim_reply(
        chat_id=-100123,
        topic_id=7,
        root_msg_id=100,
        trigger_msg_id=100,
        agent_name="kronos",
        tier=2,
        eta_ts=time.time(),
    )
    swarm.mark_sent(chat_id=-100123, topic_id=7, trigger_msg_id=100, agent_name="kronos", reply_msg_id=101)
    monkeypatch.setattr(swarm, "due_sla_watches", lambda **kwargs: [dict(row) for row in swarm.sla_watches()])
    monkeypatch.setattr("kronos.cron.escalation.get_swarm", lambda: swarm)

    await run_sla_escalation()

    assert swarm.pending_handoffs("nexus") == []
    assert swarm.sla_watches()[0]["state"] == "answered"


@pytest.mark.asyncio
async def test_an_owner_without_cover_is_dropped_not_retried_forever(swarm, monkeypatch):
    from kronos.cron.escalation import run_sla_escalation

    monkeypatch.setattr(settings, "agent_name", "kronos")
    _watch(swarm, sla_minutes=1, owner="nexus")  # nexus declares no escalates_to
    monkeypatch.setattr(swarm, "due_sla_watches", lambda **kwargs: [dict(row) for row in swarm.sla_watches()])
    monkeypatch.setattr("kronos.cron.escalation.get_swarm", lambda: swarm)

    await run_sla_escalation()

    assert swarm.sla_watches()[0]["state"] == "dropped"
    assert swarm.get_metrics().get("escalations_undeliverable") == 1


@pytest.mark.asyncio
async def test_nothing_due_is_a_cheap_no_op(swarm, monkeypatch):
    from kronos.cron.escalation import run_sla_escalation

    monkeypatch.setattr("kronos.cron.escalation.get_swarm", lambda: swarm)

    await run_sla_escalation()  # must not raise on an empty ledger
