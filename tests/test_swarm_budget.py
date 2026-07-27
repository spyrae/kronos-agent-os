"""Per-agent budgets and quiet mode (moat phase 11.3).

A swarm-wide cap says nothing about *who* spent it: the first agent awake can
burn the shared limit on unprompted opinions and leave the other five mute. A
personal slice fixes that — and it deliberately does not block, because the
thing worth protecting is the user's direct question, not the volunteering.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kronos import policy as policy_module
from kronos.config import settings
from kronos.group_router import MAX_PEER_REPLIES, GroupRouter
from kronos.security import cost_guardian as guardian_module
from kronos.security.cost_guardian import CostGuardian

MY_ID = 1001
USER_ID = 42
PEER_ID = 2001

PROFILES = {
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["kronos"],
        "role": "strategic advisor",
        "budget_usd_daily": 1.0,
    },
    "impulse": {
        "username": "impulseagnt",
        "aliases": ["impulse"],
        "role": "action catalyst",
        "max_implicit_replies": 1,
    },
}


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    from kronos.group_router import AGENT_PROFILES

    original = {name: dict(prof) for name, prof in AGENT_PROFILES.items()}
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({name: dict(prof) for name, prof in PROFILES.items()})
    monkeypatch.setattr(policy_module, "_active", policy_module.Policy(budgets={"daily_usd": 10.0}))
    yield
    AGENT_PROFILES.clear()
    AGENT_PROFILES.update(original)


def _spend(monkeypatch, **per_agent: float) -> None:
    monkeypatch.setattr(guardian_module, "_swarm_per_agent_cost", lambda: dict(per_agent))
    monkeypatch.setattr(
        guardian_module,
        "_swarm_daily_cost",
        lambda: {"cost_usd": sum(per_agent.values()), "requests": 1},
    )


def _guardian(monkeypatch, agent: str = "kronos") -> CostGuardian:
    monkeypatch.setattr(settings, "agent_name", agent)
    return CostGuardian(daily_limit=10.0, session_limit=1.0)


def _router(agent_name: str) -> GroupRouter:
    return GroupRouter(
        agent_name=agent_name,
        my_id=MY_ID,
        my_username=PROFILES[agent_name]["username"],
        allowed_user_ids={USER_ID},
    )


def _event(text: str, *, sender_id: int = USER_ID, reply_msg=None):
    event = MagicMock()
    event.raw_text = text
    event.sender_id = sender_id
    event.message = MagicMock()
    event.message.id = 100
    event.message.entities = []
    event.is_reply = reply_msg is not None
    event.get_reply_message = AsyncMock(return_value=reply_msg)
    event.topic_label = None
    return event


# --- the personal limit -------------------------------------------------------


def test_the_registry_supplies_the_limit(monkeypatch):
    guard = _guardian(monkeypatch, "kronos")

    assert guard.personal_limit() == 1.0


def test_the_policy_overrides_the_registry(monkeypatch):
    monkeypatch.setattr(
        policy_module,
        "_active",
        policy_module.Policy(budgets={"daily_usd": 10.0, "per_agent_daily_usd": {"kronos": 3.0}}),
    )
    guard = _guardian(monkeypatch, "kronos")

    assert guard.personal_limit() == 3.0


def test_no_declared_budget_means_no_personal_cap(monkeypatch):
    guard = _guardian(monkeypatch, "impulse")

    assert guard.personal_limit() == 0.0
    _spend(monkeypatch, impulse=9.99)
    assert guard.quiet_reason() == "", "without a personal slice only the swarm cap applies"


def test_spend_is_read_per_agent(monkeypatch):
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=0.4, impulse=2.0)

    assert guard.personal_spend() == 0.4
    assert guard.personal_spend("impulse") == 2.0


# --- quiet mode ---------------------------------------------------------------


def test_under_the_limit_the_agent_stays_talkative(monkeypatch):
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=0.5)

    assert guard.quiet_reason() == ""


def test_at_the_limit_the_agent_goes_quiet(monkeypatch):
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=1.0)

    assert "personal daily budget spent" in guard.quiet_reason()


def test_the_hard_gate_stays_swarm_wide(monkeypatch):
    """Quiet mode must not become a block — Tier 1 has to keep working."""
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=1.5)

    allowed, reason = guard.check_budget("session-1")

    assert allowed is True, reason


def test_an_unreadable_ledger_does_not_mute_the_agent(monkeypatch):
    """Fail open: a locked database must not read as "budget exhausted"."""
    guard = _guardian(monkeypatch, "kronos")

    def broken_swarm():
        raise RuntimeError("database is locked")

    monkeypatch.setattr("kronos.swarm_store.get_swarm", broken_swarm)

    assert guardian_module._swarm_per_agent_cost() == {}
    assert guard.quiet_reason() == ""


def test_a_broken_guardian_does_not_mute_the_router(monkeypatch):
    router = _router("kronos")

    def broken_guardian():
        raise RuntimeError("guardian unavailable")

    monkeypatch.setattr("kronos.security.cost_guardian.get_guardian", broken_guardian)

    assert router._quiet_reason() == ""


def test_the_status_reports_the_personal_slice(monkeypatch):
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=1.0)

    status = guard.get_status()

    assert status["personal_limit"] == 1.0
    assert status["personal_cost"] == 1.0
    assert status["quiet"] is True


# --- degradation --------------------------------------------------------------


def test_the_personal_share_degrades_before_the_swarm_does(monkeypatch):
    """80% of a small slice, while the shared budget is still comfortable."""
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=0.85)

    assert guard.should_degrade() is True


def test_a_comfortable_agent_does_not_degrade(monkeypatch):
    guard = _guardian(monkeypatch, "kronos")
    _spend(monkeypatch, kronos=0.5, impulse=1.0)

    assert guard.should_degrade() is False


def test_the_swarm_threshold_still_degrades_everyone(monkeypatch):
    guard = _guardian(monkeypatch, "impulse")  # no personal limit
    _spend(monkeypatch, kronos=4.0, impulse=4.5)

    assert guard.should_degrade() is True


# --- routing under quiet mode -------------------------------------------------


@pytest.mark.asyncio
async def test_a_quiet_agent_still_answers_a_direct_mention(monkeypatch):
    router = _router("kronos")
    monkeypatch.setattr(router, "_quiet_reason", lambda: "personal daily budget spent")

    decision = await router.decide(_event("@kronosagnt что делать?"), MagicMock())

    assert decision.should_respond is True
    assert decision.tier == 1


@pytest.mark.asyncio
async def test_a_quiet_agent_does_not_volunteer(monkeypatch):
    router = _router("kronos")
    monkeypatch.setattr(router, "_quiet_reason", lambda: "personal daily budget spent")
    relevance = AsyncMock(return_value=10)
    monkeypatch.setattr(router, "_check_relevance", relevance)

    decision = await router.decide(_event("вопрос без адресата"), MagicMock())

    assert decision.should_respond is False
    assert "quiet mode" in decision.reason
    relevance.assert_not_awaited(), "the check must come before the spend it prevents"


@pytest.mark.asyncio
async def test_a_quiet_agent_does_not_react_to_peers(monkeypatch):
    router = _router("kronos")
    monkeypatch.setattr(router, "_quiet_reason", lambda: "personal daily budget spent")
    react = AsyncMock(return_value=True)
    monkeypatch.setattr(router, "_should_react_to_peer", react)
    user_msg = MagicMock()
    user_msg.sender_id = USER_ID

    decision = await router.decide(_event("мнение коллеги", sender_id=PEER_ID, reply_msg=user_msg), MagicMock())

    assert decision.should_respond is False
    assert "quiet mode" in decision.reason
    react.assert_not_awaited()


# --- per-agent reply tolerance ------------------------------------------------


def test_the_reply_cap_defaults_to_the_swarm_value():
    assert _router("kronos").max_implicit_replies == MAX_PEER_REPLIES


def test_the_reply_cap_honours_the_per_agent_override():
    assert _router("impulse").max_implicit_replies == 1


@pytest.mark.asyncio
async def test_a_yielding_agent_stands_down_one_reply_sooner(monkeypatch):
    router = _router("impulse")
    monkeypatch.setattr(router, "_count_peer_replies", AsyncMock(return_value=1))

    assert await router.should_still_respond(_event("x"), MagicMock(), tier=2) is False
