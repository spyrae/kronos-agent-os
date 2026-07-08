"""Deploy-gate evals for deterministic swarm invariants.

These tests intentionally avoid network and LLM calls. They encode golden
properties that must hold before a deploy restarts production agents.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kronos.group_router import (
    AGENT_PROFILES,
    PEER_REACTION_COOLDOWN,
    GroupRouter,
)

pytestmark = pytest.mark.eval

USER_ID = 42
KRONOS_ID = 1001
NEXUS_ID = 1002
PEER_ID = 2001
OTHER_PEER_ID = 2002
CHAT_ID = -100123


@pytest.fixture(autouse=True)
def agent_profiles():
    """Install a small deterministic swarm profile set for evals."""
    original = {name: dict(profile) for name, profile in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({
        "kronos": {
            "username": "kronosagnt",
            "aliases": ["kronos"],
            "role": "primary coordinator",
        },
        "nexus": {
            "username": "nexusagnt",
            "aliases": ["nexus"],
            "role": "critical reviewer",
        },
        "operator": {
            "username": "operatoragnt",
            "aliases": ["operator"],
            "role": "execution and momentum",
        },
    })
    yield
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(original)


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    """Fresh SwarmStore backed by a temp SQLite file."""
    swarm_path = tmp_path / "swarm.db"
    db_dir = tmp_path / "agent"
    db_dir.mkdir()

    from kronos.config import settings

    monkeypatch.setattr(settings, "swarm_db_path", str(swarm_path))
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))

    from kronos import db as db_module

    db_module._instances.clear()
    import kronos.swarm_store as swarm_store

    swarm_store._singleton = None
    return swarm_store.get_swarm()


def _router(agent_name: str) -> GroupRouter:
    profile = AGENT_PROFILES[agent_name]
    my_id = {"kronos": KRONOS_ID, "nexus": NEXUS_ID}.get(agent_name, 1003)
    return GroupRouter(
        agent_name=agent_name,
        my_id=my_id,
        my_username=profile["username"],
        allowed_user_ids={USER_ID},
    )


def _event(
    *,
    text: str,
    sender_id: int,
    msg_id: int = 100,
    reply_msg=None,
):
    event = MagicMock()
    event.raw_text = text
    event.sender_id = sender_id
    event.chat_id = CHAT_ID
    event.message = MagicMock()
    event.message.id = msg_id
    event.message.entities = []
    event.is_reply = reply_msg is not None
    event.get_reply_message = AsyncMock(return_value=reply_msg)
    return event


@pytest.mark.asyncio
async def test_addressing_guard_only_target_agent_answers() -> None:
    """Golden: @nexusagnt is answered only by Nexus, not by other agents."""
    expected = {
        "kronos": (False, 0),
        "nexus": (True, 1),
        "operator": (False, 0),
    }

    for agent_name, (should_respond, tier) in expected.items():
        router = _router(agent_name)
        event = _event(text="@nexusagnt проверь риск", sender_id=USER_ID)
        with patch.object(router, "_check_relevance", new=AsyncMock(side_effect=AssertionError)):
            decision = await router.decide(event, client=MagicMock())

        assert decision.should_respond is should_respond, agent_name
        assert decision.tier == tier, agent_name
        assert decision.addressing is not None
        assert decision.addressing.target_agents == {"nexus"}


@pytest.mark.asyncio
async def test_tier_classification_explicit_and_relevance() -> None:
    explicit = await _router("kronos").decide(
        _event(text="@kronosagnt ответь коротко", sender_id=USER_ID),
        client=MagicMock(),
    )
    assert explicit.should_respond is True
    assert explicit.tier == 1

    router = _router("operator")
    with patch.object(router, "_check_relevance", new=AsyncMock(return_value=7)):
        relevant = await router.decide(
            _event(text="нужно ускорить запуск релиза", sender_id=USER_ID),
            client=MagicMock(),
        )
    assert relevant.should_respond is True
    assert relevant.tier == 2
    assert relevant.reason == "relevance=7"


@pytest.mark.asyncio
async def test_peer_reaction_requires_user_root_and_has_cooldown() -> None:
    router = _router("kronos")
    user_root = MagicMock(sender_id=USER_ID)
    first = _event(
        text="peer answer with a gap",
        sender_id=PEER_ID,
        msg_id=501,
        reply_msg=user_root,
    )
    second = _event(
        text="another peer answer",
        sender_id=PEER_ID,
        msg_id=502,
        reply_msg=user_root,
    )

    with patch.object(router, "_should_react_to_peer", new=AsyncMock(return_value=True)):
        decision = await router.decide(first, client=MagicMock())
        cooldown = await router.decide(second, client=MagicMock())

    assert PEER_REACTION_COOLDOWN == 300
    assert decision.should_respond is True
    assert decision.tier == 3
    assert cooldown.should_respond is False
    assert "cooldown" in cooldown.reason

    peer_root = MagicMock(sender_id=OTHER_PEER_ID)
    peer_chain = _event(
        text="peer-to-peer chain",
        sender_id=PEER_ID,
        msg_id=503,
        reply_msg=peer_root,
    )
    with patch.object(router, "_should_react_to_peer", new=AsyncMock(side_effect=AssertionError)):
        skipped = await _router("kronos").decide(peer_chain, client=MagicMock())
    assert skipped.should_respond is False
    assert "not replying to a user" in skipped.reason


def test_reply_claim_winner_rule_is_deterministic(swarm) -> None:
    """Golden: winner rule is tier ASC, eta_ts ASC, agent_name ASC."""
    base_eta = time.time() + 10
    for agent_name, tier, eta, trigger_id in (
        ("nexus", 2, base_eta, 11),
        ("kronos", 2, base_eta, 12),
        ("operator", 3, base_eta - 9, 13),
    ):
        swarm.claim_reply(
            chat_id=CHAT_ID,
            topic_id=None,
            root_msg_id=1,
            trigger_msg_id=trigger_id,
            agent_name=agent_name,
            tier=tier,
            eta_ts=eta,
            reason="eval",
        )

    assert swarm.can_send_claim(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=1,
        agent_name="kronos",
        tier=2,
    ).won is True
    assert swarm.can_send_claim(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=1,
        agent_name="nexus",
        tier=2,
    ).won is False
    assert swarm.can_send_claim(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=1,
        agent_name="operator",
        tier=3,
    ).won is False


def test_implicit_reply_cap_and_pass_cancellation(swarm) -> None:
    """Golden: implicit cap is <=2 per root; PASS-style cancellation releases claims."""
    for idx, agent_name in enumerate(("kronos", "nexus"), start=1):
        swarm.claim_reply(
            chat_id=CHAT_ID,
            topic_id=None,
            root_msg_id=10,
            trigger_msg_id=idx,
            agent_name=agent_name,
            tier=2,
            eta_ts=time.time() + idx,
            reason="eval",
        )
        swarm.mark_sent(
            chat_id=CHAT_ID,
            topic_id=None,
            trigger_msg_id=idx,
            agent_name=agent_name,
            reply_msg_id=100 + idx,
        )

    swarm.claim_reply(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=10,
        trigger_msg_id=3,
        agent_name="operator",
        tier=2,
        eta_ts=time.time() + 3,
        reason="eval",
    )
    capped = swarm.can_send_claim(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=10,
        agent_name="operator",
        tier=2,
    )
    assert capped.won is False
    assert "cap" in capped.reason

    swarm.claim_reply(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=20,
        trigger_msg_id=20,
        agent_name="kronos",
        tier=3,
        eta_ts=time.time(),
        reason="peer-reaction",
    )
    swarm.cancel_claim(
        chat_id=CHAT_ID,
        topic_id=None,
        trigger_msg_id=20,
        agent_name="kronos",
        reason="peer-reaction self-pass",
    )
    cancelled = swarm.can_send_claim(
        chat_id=CHAT_ID,
        topic_id=None,
        root_msg_id=20,
        agent_name="kronos",
        tier=3,
    )
    assert cancelled.won is False
    assert cancelled.reason == "no active claim"


@pytest.mark.asyncio
async def test_first_peer_reaction_survives_low_monotonic_clock(monkeypatch):
    # Regression: the Tier-3 cooldown compares against time.monotonic(), which
    # is boot-relative on Linux. On a fresh runner/host whose monotonic clock is
    # still below PEER_REACTION_COOLDOWN, the FIRST peer reaction must not be
    # falsely treated as cooled-down (this passed on high-uptime dev machines
    # but failed on a fresh CI runner).
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)
    router = _router("kronos")
    user_root = MagicMock(sender_id=USER_ID)
    evt = _event(
        text="peer answer right after restart",
        sender_id=PEER_ID,
        msg_id=777,
        reply_msg=user_root,
    )
    with patch.object(router, "_should_react_to_peer", new=AsyncMock(return_value=True)):
        decision = await router.decide(evt, client=MagicMock())

    assert decision.should_respond is True
    assert decision.tier == 3
