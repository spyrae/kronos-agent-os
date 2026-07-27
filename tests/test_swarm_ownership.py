"""Owner-first routing and SLA escalation (moat phase 11.2).

The point of ownership is that the specialist answers its own topic even when a
generalist scores the message higher — and that the specialist going silent has
a consequence instead of leaving the user unanswered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kronos.group_router import OWNER_DEFERENCE_SECONDS, GroupRouter

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


# --- topic recognition --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_labelled_event_needs_no_llm():
    """The local bus and eval scenarios name the subject directly."""
    router = _router("kronos")

    with patch.object(router, "_classify_topic", new=AsyncMock(return_value="")) as classify:
        topic = await router._topic_key(_event("что дальше?", topic_label="Planning"), "что дальше?")

    assert topic == "planning"
    classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_undeclared_label_falls_back_to_classification():
    router = _router("kronos")

    with patch.object(router, "_classify_topic", new=AsyncMock(return_value="metrics")):
        assert await router._topic_key(_event("сколько DAU?", topic_label="general"), "сколько DAU?") == "metrics"


@pytest.mark.asyncio
async def test_classification_is_cached_per_message():
    router = _router("kronos")
    reply = MagicMock()
    reply.content = "planning"
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=reply)

    with patch("kronos.llm.get_model", return_value=model):
        first = await router._classify_topic("надо распланировать квартал")
        second = await router._classify_topic("надо распланировать квартал")

    assert first == second == "planning"
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_an_invented_label_is_ignored():
    """The lite model answers freely; only a declared topic counts."""
    router = _router("kronos")
    reply = MagicMock()
    reply.content = "product-strategy"
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=reply)

    with patch("kronos.llm.get_model", return_value=model):
        assert await router._classify_topic("что делаем с продуктом?") == ""


@pytest.mark.asyncio
async def test_classification_failure_falls_back_to_no_topic():
    """A glitch in a lite call must not silence the swarm."""
    router = _router("kronos")
    model = MagicMock()
    model.ainvoke = AsyncMock(side_effect=RuntimeError("provider down"))

    with patch("kronos.llm.get_model", return_value=model):
        assert await router._classify_topic("текст") == ""


@pytest.mark.asyncio
async def test_a_registry_without_ownership_costs_nothing():
    from kronos.group_router import AGENT_PROFILES

    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({"impulse": dict(PROFILES["impulse"])})
    router = _router("impulse")

    with patch.object(router, "_classify_topic", new=AsyncMock(return_value="planning")) as classify:
        assert await router._topic_key(_event("текст"), "текст") == ""

    classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_contested_topic_has_no_owner():
    from kronos.group_router import AGENT_PROFILES

    AGENT_PROFILES["nexus"] = {**PROFILES["nexus"], "owns": ["planning"]}
    router = _router("impulse")

    assert router._topic_owners.get("planning", "") == ""


# --- owner-first routing ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_answers_without_a_relevance_check():
    router = _router("kronos")

    with patch.object(router, "_check_relevance", new=AsyncMock(return_value=1)) as relevance:
        decision = await router.decide(_event("распланируй квартал", topic_label="planning"), MagicMock())

    assert decision.should_respond is True
    assert decision.tier == 2
    assert decision.topic_owner == "kronos"
    assert "owner of 'planning'" in decision.reason
    relevance.assert_not_awaited(), "the owner must not need permission from a relevance score"


@pytest.mark.asyncio
async def test_the_owner_outranks_a_more_relevant_non_owner():
    """Arbitration orders by eta, so the owner's lane has to be the faster one."""
    owner = _router("kronos")
    other = _router("impulse")
    event = _event("распланируй квартал", topic_label="planning")

    with patch.object(owner, "_check_relevance", new=AsyncMock(return_value=3)):
        owner_decision = await owner.decide(event, MagicMock())
    with patch.object(other, "_check_relevance", new=AsyncMock(return_value=10)):
        other_decision = await other.decide(event, MagicMock())

    assert owner_decision.should_respond and other_decision.should_respond
    assert owner_decision.delay < other_decision.delay


@pytest.mark.asyncio
async def test_a_non_owner_defers_but_still_covers_a_dead_owner():
    other = _router("impulse")

    with patch.object(other, "_check_relevance", new=AsyncMock(return_value=9)):
        decision = await other.decide(_event("распланируй квартал", topic_label="planning"), MagicMock())

    assert decision.delay == OWNER_DEFERENCE_SECONDS
    assert "deferring to owner kronos" in decision.reason

    from kronos.swarm_store import CLAIM_EXPIRY_SECONDS

    assert decision.delay < CLAIM_EXPIRY_SECONDS, "a deferral past claim expiry could never win arbitration"


@pytest.mark.asyncio
async def test_an_unowned_topic_routes_exactly_as_before():
    router = _router("impulse")

    with patch.object(router, "_classify_topic", new=AsyncMock(return_value="")):
        with patch.object(router, "_check_relevance", new=AsyncMock(return_value=9)):
            decision = await router.decide(_event("что-то нейтральное"), MagicMock())

    assert decision.should_respond is True
    assert decision.topic_owner == ""
    assert 5 <= decision.delay <= 20


@pytest.mark.asyncio
async def test_an_addressed_message_skips_classification():
    """@mention is unambiguous; paying for a lite call would be waste."""
    router = _router("kronos")
    event = _event("@kronosagnt распланируй квартал")

    with patch.object(router, "_classify_topic", new=AsyncMock(return_value="planning")) as classify:
        decision = await router.decide(event, MagicMock())

    assert decision.tier == 1
    classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_skip_still_reports_the_topic_for_the_watch():
    """The SLA watch must be registered even by an agent with nothing to say."""
    other = _router("impulse")

    with patch.object(other, "_check_relevance", new=AsyncMock(return_value=2)):
        decision = await other.decide(_event("распланируй квартал", topic_label="planning"), MagicMock())

    assert decision.should_respond is False
    assert (decision.topic, decision.topic_owner, decision.owner_sla_minutes) == ("planning", "kronos", 15)
