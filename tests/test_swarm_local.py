"""The swarm without Telegram (moat phase 11.5).

The value of this bus is that it is not a second implementation: these tests
assert the production behaviours — one answer per message, owner-first, the
addressing guard — through the local transport. If the router's rules changed,
these would break too, which is the point.
"""

import pytest

from kronos.config import settings
from kronos.swarm_local import USER_ID, LocalSwarmBus

PROFILES = {
    "kronos": {
        "username": "kronosagnt",
        "aliases": ["kronos"],
        "role": "strategic advisor",
        "owns": ["planning"],
        "escalates_to": "nexus",
        "sla_minutes": 1,
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
        "max_implicit_replies": 1,
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
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    import kronos.db as _db

    _db._instances.clear()
    from kronos.swarm_store import SwarmStore

    bus = LocalSwarmBus(store=SwarmStore())
    yield bus
    _db._instances.clear()


def _keen(agent: str, text: str) -> int:
    """Everyone finds everything relevant — maximises the chance of a duplicate."""
    return 10


# --- one answer, not three ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_implicit_reply_cap_binds_across_agents(bus):
    """Three eager agents, two replies — the swarm cap, not one-per-message."""
    bus.add_agent("kronos", relevance=_keen)
    bus.add_agent("nexus", relevance=_keen)
    bus.add_agent("impulse", relevance=_keen)

    sent = await bus.user_says("что делать с ростом?")

    assert len(sent) == 2
    assert bus.store.count_sent_replies(chat_id=bus.chat_id, topic_id=bus.topic_id, root_msg_id=1001) == 2
    assert bus.store.get_metrics().get("duplicate_replies_avoided") == 1


@pytest.mark.asyncio
async def test_an_agent_with_a_tighter_cap_yields(bus):
    """impulse declares max_implicit_replies: 1, so one peer answer silences it.

    The owner's fast lane makes the order deterministic: kronos claims 1-4s,
    impulse 5-20s, so impulse always checks the ledger second.
    """
    bus.add_agent("kronos", relevance=lambda agent, text: 1)
    bus.add_agent("impulse", relevance=_keen)

    sent = await bus.user_says("распланируй квартал", topic_label="planning")

    assert [row["agent"] for row in sent] == ["kronos"]
    assert bus.store.get_metrics().get("duplicate_replies_avoided") == 1


@pytest.mark.asyncio
async def test_an_uninterested_swarm_stays_quiet(bus):
    bus.add_agent("nexus", relevance=lambda agent, text: 2)
    bus.add_agent("impulse", relevance=lambda agent, text: 3)

    assert await bus.user_says("привет") == []


@pytest.mark.asyncio
async def test_an_addressed_agent_answers_and_the_others_do_not(bus):
    bus.add_agent("nexus", relevance=_keen)
    bus.add_agent("impulse", relevance=_keen)

    sent = await bus.user_says("@impulseagnt разблокируй это")

    assert [row["agent"] for row in sent] == ["impulse"]
    assert sent[0]["tier"] == 1


@pytest.mark.asyncio
async def test_the_topic_owner_wins_against_a_keener_peer(bus):
    bus.add_agent("kronos", relevance=lambda agent, text: 1)
    bus.add_agent("impulse", relevance=_keen)

    sent = await bus.user_says("распланируй квартал", topic_label="planning")

    assert [row["agent"] for row in sent] == ["kronos"]
    assert "owner of 'planning'" in sent[0]["reason"]


@pytest.mark.asyncio
async def test_an_owned_topic_gets_a_deadline(bus):
    bus.add_agent("kronos", relevance=_keen)

    await bus.user_says("распланируй квартал", topic_label="planning")

    watch = bus.store.sla_watches()[0]
    assert (watch["topic"], watch["owner_agent"], watch["state"]) == ("planning", "kronos", "waiting")


@pytest.mark.asyncio
async def test_a_silent_owner_is_escalated_through_the_real_job(bus, monkeypatch):
    """End-to-end: local round → watch → escalation job → hand-off queue."""
    from kronos.cron.escalation import run_sla_escalation

    monkeypatch.setattr(settings, "agent_name", "impulse")
    monkeypatch.setattr("kronos.cron.escalation.get_swarm", lambda: bus.store)
    bus.add_agent("impulse", relevance=lambda agent, text: 1)  # nobody answers
    await bus.user_says("распланируй квартал", topic_label="planning")

    watch = bus.store.sla_watches()[0]
    bus.store._db.write("UPDATE sla_watch SET deadline_ts = 0 WHERE id = ?", (watch["id"],))
    await run_sla_escalation()

    assert [h["to_agent"] for h in bus.store.pending_handoffs("nexus")] == ["nexus"]
    assert bus.store.sla_watches()[0]["state"] == "escalated"


# --- peer traffic -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_reaction_attaches_to_the_user_root(bus):
    """Tier 3 spends the root message's reply budget, not a fresh one."""
    bus.add_agent("nexus", relevance=_keen)
    bus.add_agent("kronos", relevance=lambda agent, text: 1, react=lambda agent, text: True)

    first = await bus.user_says("что с метриками?")
    reaction = await bus.run_round(first[0]["reply_facts"])

    assert [row["agent"] for row in first] == ["nexus"]
    assert [row["agent"] for row in reaction] == ["kronos"]
    assert reaction[0]["tier"] == 3
    assert bus.store.count_sent_replies(chat_id=bus.chat_id, topic_id=bus.topic_id, root_msg_id=1001) == 2


@pytest.mark.asyncio
async def test_a_third_voice_on_one_message_is_refused(bus):
    """The cap covers Tier 2 and Tier 3 together."""
    bus.add_agent("nexus", relevance=_keen)
    bus.add_agent("kronos", relevance=_keen, react=lambda agent, text: True)

    first = await bus.user_says("что с метриками?")
    reaction = await bus.run_round(first[0]["reply_facts"])

    assert len(first) == 2, "both answered the user"
    assert reaction == [], "the root message's budget is spent"


@pytest.mark.asyncio
async def test_a_peer_chain_does_not_start_new_reactions(bus):
    """Tier 3 needs a user anchor, or the bots would debate each other forever."""
    bus.add_agent("nexus", relevance=_keen, react=lambda agent, text: True)
    bus.add_agent("kronos", relevance=lambda agent, text: 1, react=lambda agent, text: True)

    first = await bus.user_says("что с метриками?")
    reaction = await bus.run_round(first[0]["reply_facts"])
    # kronos replied to nexus, so nexus sees a reply-to-me: that is Tier 1, not
    # a new Tier 3 reaction.
    chain = await bus.run_round(reaction[0]["reply_facts"])

    assert [row["tier"] for row in chain] == [1]


@pytest.mark.asyncio
async def test_a_peer_ping_pong_runs_out_of_budget(bus):
    """Found by this bus: Tier 1 bypasses every cap, so two agents looped forever.

    A peer replying to my message reads as an explicit address, and my answer is
    a reply to them — so before the exchange bound this ran without end, burning
    budget and flooding the chat.
    """
    from kronos.group_router import MAX_PEER_EXCHANGES

    bus.add_agent("nexus", relevance=_keen, react=lambda agent, text: True)
    bus.add_agent("kronos", relevance=lambda agent, text: 1, react=lambda agent, text: True)

    facts = (await bus.user_says("что с метриками?"))[0]["reply_facts"]
    hops = 0
    for _ in range(20):
        sent = await bus.run_round(facts)
        if not sent:
            break
        hops += 1
        facts = sent[0]["reply_facts"]

    assert hops < 20, "the exchange must terminate on its own"
    # One Tier 3 reaction plus a bounded exchange for each of the two agents.
    assert hops <= 1 + 2 * MAX_PEER_EXCHANGES


@pytest.mark.asyncio
async def test_an_agent_never_answers_itself(bus):
    bus.add_agent("nexus", relevance=_keen)

    sent = await bus.user_says("вопрос")
    echo = await bus.run_round(sent[0]["reply_facts"])

    assert echo == []


# --- the ledger is real -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_transcript_and_the_ledger_agree(bus):
    bus.add_agent("nexus", relevance=_keen)

    await bus.user_says("вопрос про метрики")

    assert [entry["from"] for entry in bus.transcript] == ["user", "nexus"]
    recorded = bus.store.get_recent_messages(chat_id=bus.chat_id, topic_id=bus.topic_id, limit=10)
    assert {row["sender_type"] for row in recorded} == {"user", "agent"}


@pytest.mark.asyncio
async def test_the_user_id_is_whitelisted_for_every_agent(bus):
    agent = bus.add_agent("nexus")

    assert USER_ID in agent.router.allowed_user_ids
