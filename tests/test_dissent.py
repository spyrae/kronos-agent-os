"""Mandatory review before answering (moat phase 11.4).

The properties that matter: an objection reaches the user with the answer, a
silent reviewer never eats the answer, and a swarm that did not ask for dissent
pays nothing.
"""

from unittest.mock import AsyncMock

import pytest

from kronos.config import settings
from kronos.dissent import (
    UNREVIEWED_MARK,
    classify_verdict,
    format_reviewed_answer,
    pick_reviewer,
    review_before_send,
    run_challenge_intake,
)
from kronos.swarm_config import AgentProfile

PROFILES = {
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["kronos"],
        "role": "strategic advisor",
        "owns": ["planning"],
        "escalates_to": "keystone",
        "dissent": "require",
    },
    "keystone": {
        "username": "keystoneagnt",
        "aliases": ["keystone"],
        "role": "quality engineer",
    },
    "impulse": {
        "username": "impulseagnt",
        "aliases": ["impulse"],
        "role": "action catalyst",
    },
}


@pytest.fixture(autouse=True)
def registry():
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
    # Production waits 3s between ledger reads; tests should not.
    monkeypatch.setattr("kronos.dissent.DISSENT_POLL_SECONDS", 0.01)
    import kronos.db as _db

    _db._instances.clear()
    from kronos.swarm_store import SwarmStore

    store = SwarmStore()
    monkeypatch.setattr("kronos.dissent.get_swarm", lambda: store)
    yield store
    _db._instances.clear()


async def _review(answer: str = "Делаем A, потом B.", **kwargs) -> str:
    return await review_before_send(
        answer=answer,
        chat_id=-100123,
        topic_id=7,
        thread_id="-100123:7",
        root_msg_id=500,
        topic="planning",
        author_agent="kronos",
        **kwargs,
    )


# --- pure helpers -------------------------------------------------------------


def test_the_reviewer_is_the_declared_counterpart():
    profiles = {
        "kronos": AgentProfile(username="a", escalates_to="keystone"),
        "keystone": AgentProfile(username="b"),
        "impulse": AgentProfile(username="c"),
    }

    assert pick_reviewer(profiles, "kronos") == "keystone"


def test_without_a_counterpart_any_other_agent_reviews():
    profiles = {"kronos": AgentProfile(username="a"), "impulse": AgentProfile(username="c")}

    assert pick_reviewer(profiles, "kronos") == "impulse"


def test_a_solo_agent_has_nobody_to_ask():
    assert pick_reviewer({"kronos": AgentProfile(username="a")}, "kronos") == ""


@pytest.mark.parametrize(
    "response,expected",
    [
        ("Согласен, добавить нечего.", "agree"),
        ("  согласен  ", "agree"),
        ("**Согласен** с выводом", "agree"),
        ("Возражение: сроки нереальны.", "challenge"),
        ("Не согласен — нет данных.", "challenge"),
    ],
)
def test_verdict_classification(response, expected):
    assert classify_verdict(response) == expected


def test_agreement_leaves_the_answer_alone():
    """Silence is the signal: no mark means it passed a second pair of eyes."""
    answer = format_reviewed_answer("Ответ", reviewer="keystone", verdict="agree", response="Согласен")

    assert answer == "Ответ"


def test_an_objection_travels_with_the_answer():
    answer = format_reviewed_answer(
        "Ответ",
        reviewer="keystone",
        verdict="challenge",
        response="Возражение: нет бюджета.",
    )

    assert "⚖️ Возражение от keystone" in answer
    assert "нет бюджета" in answer


# --- the gate -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_that_did_not_ask_for_dissent_pays_nothing(swarm):
    result = await review_before_send(
        answer="Ответ",
        chat_id=-100123,
        topic_id=7,
        thread_id="-100123:7",
        root_msg_id=500,
        topic="blockers",
        author_agent="impulse",
    )

    assert result == "Ответ"
    assert swarm.challenges() == [], "no review was requested, so no row"


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_reviewed(swarm):
    assert await _review(answer="   ") == "   "
    assert swarm.challenges() == []


@pytest.mark.asyncio
async def test_an_objection_is_appended_to_the_answer(swarm):
    async def reviewer_answers():
        # Give the gate one poll cycle, then answer as the reviewer would.
        import asyncio

        for _ in range(50):
            pending = swarm.challenges()
            if pending:
                swarm.answer_challenge(pending[0]["id"], verdict="challenge", response="Возражение: нет бюджета.")
                return
            await asyncio.sleep(0.01)

    import asyncio

    task = asyncio.create_task(reviewer_answers())
    result = await _review(timeout=5)
    await task

    assert "Возражение: нет бюджета." in result
    assert swarm.get_metrics().get("dissent_reviews_challenge") == 1


@pytest.mark.asyncio
async def test_a_silent_reviewer_does_not_eat_the_answer(swarm):
    result = await _review(timeout=0.05)

    assert result.startswith("Делаем A, потом B.")
    assert UNREVIEWED_MARK in result
    assert swarm.challenges()[0]["state"] == "timeout"
    assert swarm.get_metrics().get("dissent_timeouts") == 1


@pytest.mark.asyncio
async def test_a_verdict_landing_at_the_deadline_is_not_lost(swarm, monkeypatch):
    """The timeout is a compare-and-set, so a last-instant review still counts."""
    challenge_ids = []
    original = swarm.request_challenge

    def capture(**kwargs):
        challenge_id = original(**kwargs)
        challenge_ids.append(challenge_id)
        return challenge_id

    monkeypatch.setattr(swarm, "request_challenge", capture)

    real_timeout = swarm.timeout_challenge

    def answer_first(challenge_id):
        swarm.answer_challenge(challenge_id, verdict="challenge", response="Возражение: поздно, но по делу.")
        return real_timeout(challenge_id)

    monkeypatch.setattr(swarm, "timeout_challenge", answer_first)

    result = await _review(timeout=0.05)

    assert "поздно, но по делу" in result
    assert UNREVIEWED_MARK not in result
    assert swarm.get_challenge(challenge_ids[0])["state"] == "answered"


@pytest.mark.asyncio
async def test_a_solo_swarm_sends_without_review(swarm):
    from kronos.group_router import AGENT_PROFILES

    AGENT_PROFILES.clear()
    AGENT_PROFILES.update({"kronos": dict(PROFILES["kronos"])})

    result = await _review()

    assert result == "Делаем A, потом B."
    assert swarm.challenges() == []


# --- the reviewer side --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reviewer_records_a_verdict(swarm):
    challenge_id = swarm.request_challenge(
        chat_id=-100123,
        topic_id=7,
        thread_id="-100123:7",
        root_msg_id=500,
        topic="planning",
        author_agent="kronos",
        reviewer_agent="keystone",
        claim="Делаем A, потом B.",
    )

    class Reviewer:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, **kwargs):
            self.calls.append(kwargs)
            return "Возражение: B невозможно без C."

    reviewer = Reviewer()
    handled = await run_challenge_intake(reviewer, swarm, reviewer_agent="keystone")

    assert handled == 1
    row = swarm.get_challenge(challenge_id)
    assert row["state"] == "answered"
    assert row["verdict"] == "challenge"
    assert "B невозможно без C" in row["response"]
    # A review is not the user's turn: it must not enter the reviewer's history.
    assert reviewer.calls[0]["source_kind"] == "peer_reaction"
    assert reviewer.calls[0]["persist_user_turn"] is False


@pytest.mark.asyncio
async def test_a_reviewer_only_sees_its_own_queue(swarm):
    swarm.request_challenge(
        chat_id=1,
        topic_id=0,
        thread_id="1",
        root_msg_id=1,
        topic="planning",
        author_agent="kronos",
        reviewer_agent="keystone",
        claim="x",
    )

    handled = await run_challenge_intake(AsyncMock(), swarm, reviewer_agent="impulse")

    assert handled == 0


@pytest.mark.asyncio
async def test_a_claimed_challenge_is_not_handed_out_twice(swarm):
    swarm.request_challenge(
        chat_id=1,
        topic_id=0,
        thread_id="1",
        root_msg_id=1,
        topic="planning",
        author_agent="kronos",
        reviewer_agent="keystone",
        claim="x",
    )

    first = swarm.accept_next_challenge("keystone")
    second = swarm.accept_next_challenge("keystone")

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_a_failed_review_leaves_the_row_for_the_ledger(swarm):
    challenge_id = swarm.request_challenge(
        chat_id=1,
        topic_id=0,
        thread_id="1",
        root_msg_id=1,
        topic="planning",
        author_agent="kronos",
        reviewer_agent="keystone",
        claim="x",
    )

    class Broken:
        async def ainvoke(self, **kwargs):
            raise RuntimeError("model down")

    handled = await run_challenge_intake(Broken(), swarm, reviewer_agent="keystone")

    assert handled == 1
    assert swarm.get_challenge(challenge_id)["state"] == "reviewing"


def test_an_unknown_verdict_is_rejected(swarm):
    challenge_id = swarm.request_challenge(
        chat_id=1,
        topic_id=0,
        thread_id="1",
        root_msg_id=1,
        topic="planning",
        author_agent="kronos",
        reviewer_agent="keystone",
        claim="x",
    )

    with pytest.raises(ValueError):
        swarm.answer_challenge(challenge_id, verdict="maybe", response="")
