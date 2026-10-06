"""A06: one message through the whole chain — chat, tool, approval, effect, delivery.

Real engine, session store, intent ledger, outbox and budget ledger. Fakes only
at the three edges that leave the machine: the model, the tool's external
system and the Telegram sender. Each fault test breaks one edge and checks that
the chain neither repeats the effect nor reports something that did not happen.
"""

import sqlite3
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.effect_state import DurableStateError
from kronos.graph import KronosAgent
from kronos.memory.scope import current_data_owner
from kronos.security import cost_guardian
from kronos.security.cost_tracking import record_llm_cost
from kronos.security.effects import mark_side_effect
from kronos.security.model_budget import BudgetedModel
from kronos.session import SessionStore
from kronos.turn_delivery import RecoveryDestination
from kronos.turn_ownership import own_conversation

CHAT, OWNER, BOT_ACCOUNT = 77, "101", 55
ROUTE = RecoveryDestination(chat_id=CHAT, sender_id=BOT_ACCOUNT)


class ExternalSystem:
    """The world outside: counts what really happened, whatever the agent believes."""

    def __init__(self):
        self.reports: list[str] = []
        self.owners: list[str] = []
        self.fail_after_sending = False

    async def send_report(self, text: str) -> str:
        self.owners.append(current_data_owner())
        self.reports.append(text)
        if self.fail_after_sending:
            raise TimeoutError("no answer from the report service")
        return f"report #{len(self.reports)} accepted"


class Telegram:
    def __init__(self):
        self.sent: list[tuple[int, str, int]] = []
        self.fail_next = 0

    async def send(self, chunk):
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("telegram unreachable")
        self.sent.append((chunk.chat_id, chunk.text, chunk.random_id))
        return 1000 + len(self.sent)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    guardian = cost_guardian.CostGuardian(daily_limit=5.0, session_limit=1.0)
    monkeypatch.setattr(cost_guardian, "_guardian", guardian)
    monkeypatch.setattr(guardian, "should_degrade", lambda: False)
    external, telegram = ExternalSystem(), Telegram()
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: BOT_ACCOUNT)
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", telegram.send)
    yield external, telegram, monkeypatch
    db_module._instances.clear()
    swarm_module._singleton = None


def _agent(world, responses):
    external, _, monkeypatch = world
    queue = list(responses)

    async def respond(*args, **kwargs):
        # A provider that reports usage, so the call's budget hold is settled.
        record_llm_cost("scripted", 100, 20, 0.001)
        return queue.pop(0)

    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=respond)
    model.bind_tools = lambda tools: model
    # Wrapped exactly as the model factory wraps a real provider.
    budgeted = BudgetedModel(model, label="standard", lite_factory=lambda: model)
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: budgeted)
    agent = object.__new__(KronosAgent)
    agent._session_store = SessionStore(settings.db_path, agent_name="kronos")
    agent._memory_enabled = False
    agent._supervisor = None
    agent._skill_store = None
    agent._system_prompt = "system"
    agent._external_tool_event_callback = None
    agent._durable_recovery_checked = True
    agent._last_pending_approval_id = None
    agent._tools = mark_side_effect(
        [StructuredTool.from_function(coroutine=external.send_report, name="send_report", description="send")]
    )
    return agent, model


def _call(text="Q3 numbers", call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": "send_report", "args": {"text": text}, "id": call_id}])


async def _ask(agent, text="send the Q3 report"):
    """What the Telegram bridge does: a durable user turn whose reply it owes."""
    return await agent.ainvoke_outcome(
        text, str(CHAT), user_id=OWNER, session_id=str(CHAT), recovery_destination=ROUTE, reply_owed=True
    )


async def _restart(agent):
    """A new process: fresh store object, the same files."""
    store = SessionStore(settings.db_path, agent_name="kronos")
    agent._session_store = store
    await store.adopt_unacknowledged_replies()
    return store


def _rows(table, where="1=1"):
    with sqlite3.connect(settings.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where}")]


# --- the whole chain ------------------------------------------------------------


async def test_message_to_delivered_answer_with_one_effect(world):
    external, telegram, _ = world
    agent, model = _agent(world, [_call(), AIMessage(content="The Q3 report is sent.")])

    # 1. chat → model → tool call: the effect waits for a human.
    paused = await _ask(agent)
    assert paused.status == "waiting_approval" and paused.approval_id
    assert external.reports == [], "nothing leaves the machine before approval"

    # 2. approval by the owner → the effect runs once, through the intent ledger.
    answer = await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner-device")
    assert answer == "The Q3 report is sent."
    assert external.reports == ["Q3 numbers"]
    assert external.owners == [OWNER], "the approver decides; the data owner is still the asker"
    assert [e["tool"] for e in _rows("external_effects")] == ["send_report"]
    assert _rows("effect_intents", "status = 'pending'") == []
    done = await agent.get_turn_outcome(paused.turn_id)
    assert done.status == "completed" and done.content == answer

    # 3. the process dies before the bridge sends the answer → recovered on restart.
    store = await _restart(agent)
    assert await store.deliver_pending() == 1
    assert len(telegram.sent) == 1
    chat_id, text, _ = telegram.sent[0]
    assert chat_id == CHAT and "The Q3 report is sent." in text
    assert (await store.delivery_status(paused.turn_id))["state"] == "delivered"

    # 4. nothing in the chain runs twice.
    await _restart(agent)
    assert await store.deliver_pending() == 0
    assert external.reports == ["Q3 numbers"] and len(telegram.sent) == 1
    assert model.ainvoke.await_count == 2

    # 5. the cost of both model calls is settled and the conversation is searchable by its owner only.
    from kronos.swarm_store import get_swarm

    assert get_swarm().budget_holds() == {"held": 0.0, "unknown": 0.0}
    assert get_swarm().session_cost(str(CHAT)) == pytest.approx(0.002)
    assert get_swarm().search_sessions(query="report", user_id=OWNER)
    assert get_swarm().search_sessions(query="report", user_id="someone-else") == []


async def test_a_rejected_effect_never_happens_and_the_user_is_still_answered(world):
    external, telegram, _ = world
    agent, _ = _agent(world, [_call(), AIMessage(content="Understood, nothing was sent.")])

    paused = await _ask(agent)
    answer = await agent.resolve_tool_approval(paused.approval_id, approved=False, decided_by="owner-device")

    assert external.reports == [] and _rows("external_effects") == []
    assert answer == "Understood, nothing was sent."
    assert (await agent.get_turn_outcome(paused.turn_id)).status == "rejected"
    store = await _restart(agent)
    await store.deliver_pending()
    assert len(telegram.sent) == 1 and "nothing was sent" in telegram.sent[0][1]


# --- one broken edge at a time ---------------------------------------------------


async def test_effect_that_times_out_after_dispatch_is_never_repeated_or_called_done(world):
    external, telegram, _ = world
    external.fail_after_sending = True
    agent, model = _agent(world, [_call(), AIMessage(content="The Q3 report is sent.")])
    paused = await _ask(agent)

    with pytest.raises(DurableStateError):
        await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner-device")

    # The report did go out, the agent cannot know it, and it must not claim either way.
    assert external.reports == ["Q3 numbers"]
    outcome = await agent.get_turn_outcome(paused.turn_id)
    assert outcome.status == "failed" and outcome.content != "The Q3 report is sent."
    store = agent._session_store
    pending = await store.pending_effects()
    assert [p["tool"] for p in pending] == ["send_report"]

    # A restart and an explicit resume do not send it again.
    external.fail_after_sending = False
    store = await _restart(agent)
    assert await agent.resume_interrupted_turn(paused.turn_id, notify=False) is None
    assert external.reports == ["Q3 numbers"] and model.ainvoke.await_count == 1

    # The user is told the turn did not finish — not that the report was sent.
    await store.deliver_pending()
    assert len(telegram.sent) == 1 and "не завершено" in telegram.sent[0][1]
    assert "report is sent" not in telegram.sent[0][1]

    # Only the operator closes the uncertainty, and the verdict is kept.
    async with own_conversation(store.db_path, str(CHAT), wait=False) as ownership:
        await store.reconcile_effect(
            pending[0]["idempotency_key"],
            outcome="happened",
            result="report service shows 1 report at 10:04",
            operator="roman",
            ownership=ownership,
        )
    assert await store.pending_effects() == []
    assert (await store.effect_reconciliations(paused.turn_id))[0]["outcome"] == "happened"
    assert external.reports == ["Q3 numbers"]


async def test_telegram_outage_delays_the_answer_without_duplicating_it(world):
    external, telegram, _ = world
    agent, _ = _agent(world, [_call(), AIMessage(content="The Q3 report is sent.")])
    paused = await _ask(agent)
    await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner-device")
    store = await _restart(agent)

    telegram.fail_next = 1
    assert await store.deliver_pending() == 0
    assert telegram.sent == [] and (await store.delivery_status(paused.turn_id))["state"] == "pending"

    with sqlite3.connect(settings.db_path) as db:
        identity = db.execute("SELECT random_ids FROM delivery_outbox WHERE obsolete = 0").fetchone()[0]
        db.execute("UPDATE delivery_outbox SET next_attempt = 0")
    assert await store.deliver_pending() == 1

    assert len(telegram.sent) == 1 and str(telegram.sent[0][2]) in identity, "the retry reuses the message identity"
    assert external.reports == ["Q3 numbers"], "a delivery retry is not an effect retry"


async def test_a_confirmed_reply_is_not_sent_again_after_a_restart(world):
    external, telegram, _ = world
    agent, _ = _agent(world, [AIMessage(content="It is 4 pm.")])

    outcome = await _ask(agent, "what time is it?")
    assert outcome.status == "completed"
    await agent._session_store.settle_reply(outcome.turn_id, "sent")  # the bridge sent it

    store = await _restart(agent)
    assert await store.deliver_pending() == 0 and telegram.sent == []


async def test_an_exhausted_budget_stops_the_chain_before_any_effect(world):
    external, telegram, monkeypatch = world
    monkeypatch.setattr(cost_guardian, "_guardian", cost_guardian.CostGuardian(daily_limit=0.0, session_limit=1.0))
    agent, model = _agent(world, [_call(), AIMessage(content="The Q3 report is sent.")])

    outcome = await _ask(agent)

    assert outcome.status not in {"completed", "waiting_approval"}
    assert external.reports == [] and _rows("effect_intents") == []
    model.ainvoke.assert_not_awaited()


async def test_a_second_approval_of_the_same_request_does_not_run_the_effect_again(world):
    external, _, _ = world
    agent, _ = _agent(world, [_call(), AIMessage(content="The Q3 report is sent.")])
    paused = await _ask(agent)

    await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner-device")
    again = await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner-device")

    assert external.reports == ["Q3 numbers"]
    assert "уже обработан" in again
