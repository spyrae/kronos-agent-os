"""F12: the budget is reserved atomically, survives a restart and never forgets an unknown outcome."""

import asyncio
import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos.audit import reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings
from kronos.security import cost_guardian, model_budget
from kronos.security.cost_guardian import BudgetRefusedError, CostGuardian
from kronos.security.cost_tracking import record_llm_cost
from kronos.security.model_budget import BudgetedModel, ModelBudgetError, admit_model_call, current_hold


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    guardian = CostGuardian(daily_limit=1.0, session_limit=0.1)
    monkeypatch.setattr(cost_guardian, "_guardian", guardian)
    monkeypatch.setattr(guardian, "should_degrade", lambda: False)
    store = swarm_module.get_swarm()
    yield store
    db_module._instances.clear()
    swarm_module._singleton = None


def _scope(session="chat"):
    return set_tool_audit_context(agent="kronos", session_id=session, thread_id=session)


def _reserve(swarm, session="chat", estimate=0.02, daily=1.0, per_session=0.1):
    return swarm.reserve_budget(
        session_key=session, agent="kronos", estimate_usd=estimate, daily_limit=daily, session_limit=per_session
    )


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"provider said {status_code}")
        self.status_code = status_code


# --- atomic reservation -------------------------------------------------------


def test_concurrent_calls_cannot_all_see_the_same_remaining_budget(swarm):
    """Ten threads, room for five: a check-then-spend would admit all ten."""
    admitted, barrier = [], threading.Barrier(10)

    def call():
        barrier.wait()
        hold, _ = _reserve(swarm, session="", daily=0.10)
        if hold:
            admitted.append(hold)

    threads = [threading.Thread(target=call) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 5 and len(set(admitted)) == 5
    assert swarm.budget_holds()["held"] == pytest.approx(0.10)


def test_a_second_process_sees_the_holds_of_the_first(swarm, tmp_path):
    from kronos.swarm_store import SwarmStore

    for _ in range(5):
        assert _reserve(swarm, session="", daily=0.10)[0]
    other_process = SwarmStore()
    hold, reason = _reserve(other_process, session="", daily=0.10)
    assert hold == "" and "in flight" in reason


def test_session_limit_counts_recorded_spend_and_holds_together(swarm):
    swarm.add_session_cost("chat", 0.05)
    assert _reserve(swarm)[0] and _reserve(swarm)[0]
    hold, reason = _reserve(swarm)
    assert hold == "" and "Session cost limit" in reason
    assert _reserve(swarm, session="another-chat")[0], "one conversation's cap is not another's"


def test_settling_replaces_the_estimate_with_the_real_cost(swarm):
    hold, _ = _reserve(swarm)
    swarm.settle_cost(
        agent="kronos", session_key="chat", cost_usd=0.003, input_tokens=10, output_tokens=5, hold_id=hold
    )

    assert swarm.budget_holds() == {"held": 0.0, "unknown": 0.0}
    assert swarm.session_cost("chat") == pytest.approx(0.003)
    assert swarm.daily_cost()["cost_usd"] == pytest.approx(0.003)


# --- durability ----------------------------------------------------------------


def test_session_spend_survives_a_restart(swarm, monkeypatch):
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    token = _scope()
    try:
        admit_model_call()
        record_llm_cost("model", 10, 5, 0.09)
    finally:
        reset_tool_audit_context(token)

    db_module._instances.clear()
    swarm_module._singleton = None
    monkeypatch.setattr(cost_guardian, "_guardian", CostGuardian(daily_limit=1.0, session_limit=0.1))

    token = _scope()
    try:
        with pytest.raises(ModelBudgetError, match="Session cost limit"):
            admit_model_call()
    finally:
        reset_tool_audit_context(token)


def test_limits_reset_with_the_utc_day_not_with_the_process(swarm):
    swarm.add_session_cost("chat", 0.5)
    _reserve(swarm, session="chat", per_session=10)
    with sqlite3.connect(settings.swarm_db_path) as db:
        db.execute("UPDATE session_costs SET day = '2020-01-01'")
        db.execute("UPDATE budget_holds SET day = '2020-01-01'")
    assert swarm.session_cost("chat") == 0.0
    assert _reserve(swarm)[0]
    with sqlite3.connect(settings.swarm_db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM budget_holds WHERE day = '2020-01-01'").fetchone()[0] == 0


# --- unknown outcomes ----------------------------------------------------------


def _model(side_effect):
    inner = AsyncMock()
    inner.ainvoke = AsyncMock(side_effect=side_effect)
    return BudgetedModel(inner, label="standard", lite_factory=lambda: inner)


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionResetError(), StatusError(500), StatusError(503)])
async def test_a_call_with_an_unknown_billing_outcome_keeps_its_hold(swarm, failure):
    token = _scope()
    try:
        with pytest.raises(type(failure)):
            await _model(failure).ainvoke([HumanMessage(content="hi")])
    finally:
        reset_tool_audit_context(token)
    assert swarm.budget_holds() == {"held": 0.0, "unknown": pytest.approx(0.02)}
    assert swarm.session_cost("chat") == pytest.approx(0.02)


async def test_a_cancelled_call_keeps_its_hold(swarm):
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    token = _scope()
    try:
        task = asyncio.create_task(_model(hang).ainvoke([HumanMessage(content="hi")]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reset_tool_audit_context(token)
    assert swarm.budget_holds()["unknown"] == pytest.approx(0.02)


@pytest.mark.parametrize("status", [400, 401, 429])
async def test_a_provider_refusal_that_was_not_billed_releases_its_hold(swarm, status):
    token = _scope()
    try:
        with pytest.raises(StatusError):
            await _model(StatusError(status)).ainvoke([HumanMessage(content="hi")])
    finally:
        reset_tool_audit_context(token)
    assert swarm.budget_holds() == {"held": 0.0, "unknown": 0.0}


async def test_a_success_that_reported_no_usage_is_unknown_not_free(swarm):
    token = _scope()
    try:
        await _model([AIMessage(content="ok")]).ainvoke([HumanMessage(content="hi")])
    finally:
        reset_tool_audit_context(token)
    assert swarm.budget_holds()["unknown"] == pytest.approx(0.02)


async def test_a_recorded_call_leaves_no_hold(swarm):
    async def answer(*args, **kwargs):
        record_llm_cost("model", 10, 5, 0.004)
        return AIMessage(content="ok")

    token = _scope()
    try:
        await _model(answer).ainvoke([HumanMessage(content="hi")])
    finally:
        reset_tool_audit_context(token)
    assert swarm.budget_holds() == {"held": 0.0, "unknown": 0.0}
    assert swarm.session_cost("chat") == pytest.approx(0.004)


def test_unknown_outcomes_accumulate_until_they_close_the_session(swarm):
    token = _scope()
    try:
        for _ in range(5):
            admit_model_call()
            model_budget.finish_hold(current_hold(), TimeoutError())
        with pytest.raises(ModelBudgetError, match="Session cost limit"):
            admit_model_call()
    finally:
        reset_tool_audit_context(token)


def test_a_failed_ledger_write_keeps_the_money_counted(swarm, monkeypatch):
    token = _scope()
    try:
        admit_model_call()
        monkeypatch.setattr(
            swarm, "settle_cost", lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        )
        record_llm_cost("model", 10, 5, 0.004)
    finally:
        reset_tool_audit_context(token)
    assert swarm.budget_holds()["held"] == pytest.approx(0.02)


# --- fail closed -----------------------------------------------------------------


def test_an_unreadable_ledger_refuses_the_call(swarm, monkeypatch):
    monkeypatch.setattr(
        swarm, "reserve_budget", lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    )
    with pytest.raises(BudgetRefusedError, match="unavailable"):
        cost_guardian.get_guardian().reserve("chat")
    token = _scope()
    try:
        with pytest.raises(ModelBudgetError, match="unavailable"):
            admit_model_call()
    finally:
        reset_tool_audit_context(token)


@pytest.mark.parametrize("limit", [float("nan"), float("inf"), -1.0, True])
def test_an_invalid_limit_refuses_instead_of_meaning_unlimited(swarm, limit):
    with pytest.raises(BudgetRefusedError, match="Invalid cost limit"):
        CostGuardian(daily_limit=limit, session_limit=0.1).reserve("chat")


@pytest.mark.parametrize("value", [-0.01, float("nan"), float("inf")])
def test_policy_rejects_a_meaningless_reservation(value):
    from kronos.policy import BudgetPolicy

    with pytest.raises(ValueError):
        BudgetPolicy(reservation_usd=value)
    assert BudgetPolicy().reservation_usd == 0.02 and BudgetPolicy(reservation_usd=0).reservation_usd == 0


# --- the turn's own conversation -------------------------------------------------


async def test_resume_and_approval_charge_the_conversation_that_started_the_turn(tmp_path):
    from kronos.session import SessionStore

    store = SessionStore(str(tmp_path / "session.db"))
    turn = await store.begin_turn("77:5", "question", budget_session="77")
    legacy = await store.begin_turn("88:9", "question")
    assert await store.turn_budget_session(turn, "77:5") == "77"
    assert await store.turn_budget_session(legacy, "88:9") == "88:9"
    assert await store.turn_budget_session("missing", "fallback") == "fallback"


async def test_migration_keeps_legacy_turns_on_their_thread(tmp_path):
    import aiosqlite

    from kronos.migrations.v012_turn_budget_session import migrate

    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE active_turns (turn_id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO active_turns VALUES ('old', 'running')")
    async with aiosqlite.connect(path) as db:
        await migrate(db)
        await migrate(db)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT budget_session FROM active_turns").fetchone()[0] == ""


def test_status_reports_money_in_flight_and_unknown(swarm):
    first, _ = _reserve(swarm)
    _reserve(swarm)
    swarm.mark_budget_hold_unknown(first)
    status = cost_guardian.get_guardian().get_status()
    assert status["holds"] == {"held": pytest.approx(0.02), "unknown": pytest.approx(0.02)}
