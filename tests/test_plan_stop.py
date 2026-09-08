"""A stop revokes future calls without undoing or guessing in-flight effects."""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from dashboard.api import plans as api
from kronos import plans
from kronos.config import settings
from kronos.cron import plans as poller
from kronos.effect_state import DurableStateError
from kronos.execution_control import ExecutionStoppedError
from kronos.turn_ownership import own_conversation
from tests.test_invocation_outcomes import _model, _request, _tool
from tests.test_invocation_outcomes import agent as agent


@pytest.fixture(autouse=True)
def runtime(agent, monkeypatch):
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 0)
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    monkeypatch.setattr("kronos.bridge.get_agent", lambda: agent)
    monkeypatch.setattr("kronos.bridge.deliver_plan_approval", AsyncMock(return_value=True))


def new_step():
    p = plans.create_plan(agent_name="kronos", goal="stop safely", chat_id=77)
    return p, plans.add_step(p, "do work")


def stop(p, kind):
    if kind == "cancelled":
        assert plans.cancel_plan(p, "kronos")
    else:
        plans._db().write("UPDATE plans SET expires_at=0 WHERE id=?", (p,))
        plans.expire_plan(p)


async def execute(p, s):
    await poller._run_step(plans.get_plan(p), plans.get_step(s), "")


@pytest.mark.parametrize("kind", ["cancelled", "expired"])
async def test_stop_during_model_prevents_tools_and_waits_for_live_owner(agent, monkeypatch, kind):
    effect = _tool(agent)
    entered, release = asyncio.Event(), asyncio.Event()

    async def model_call(messages):
        entered.set()
        await release.wait()
        return _request()

    model = _model(monkeypatch, model_call)
    p, s = new_step()
    task = asyncio.create_task(execute(p, s))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        stop(p, kind)
        await poller._reconcile_stops()
        step = plans.get_step(s)
        assert step["state"] == plans.STEP_RUNNING
        assert not step["stop_reconciled"]
        assert api._plan_view(plans.get_plan(p))["stop_pending_count"] == 1
        assert plans.plans_awaiting_summary("kronos") == []
    finally:
        release.set()
        await task
    await poller._reconcile_stops()
    step = plans.get_step(s)
    assert step["state"] == plans.STEP_FAILED and step["stop_reconciled"]
    assert (await agent.session_store.get_turn_detail(step["turn_id"]))["error"] == f"plan_{kind}"
    assert api._plan_view(plans.get_plan(p))["stop_pending_count"] == 0
    model.ainvoke.assert_awaited_once()
    effect.assert_not_awaited()
    if kind == "expired":
        await poller._deliver_pending_summaries(2)
        # A stopped plan's summary is deterministic, never another model/tool invocation.
        model.ainvoke.assert_awaited_once()
        assert "не завершено" in plans.get_plan(p)["summary"]


@pytest.mark.parametrize("kind", ["cancelled", "expired"])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_inflight_effect_is_recorded_or_reviewed_never_undone_or_repeated(agent, monkeypatch, kind, uncertain):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(target: str) -> str:
        calls.append(target)
        entered.set()
        await release.wait()
        if uncertain:
            raise TimeoutError("unknown upstream result")
        return "written"

    agent._tools = [StructuredTool.from_function(coroutine=write, name="send_message", description="fake effect")]
    request = AIMessage(
        content="",
        tool_calls=[
            {"name": "send_message", "args": {"target": "first"}, "id": "a"},
            {"name": "send_message", "args": {"target": "second"}, "id": "b"},
        ],
    )
    model = _model(monkeypatch, [request])
    p, s = new_step()
    task = asyncio.create_task(execute(p, s))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        stop(p, kind)
        await poller._reconcile_stops()
        assert not plans.get_step(s)["stop_reconciled"]
    finally:
        release.set()
        await task
    await poller._reconcile_stops()
    step = plans.get_step(s)
    assert step["state"] == (plans.STEP_REVIEW if uncertain else plans.STEP_FAILED)
    effects = await agent.session_store.list_external_effects(step["turn_id"])
    assert len(effects) == 1
    assert effects[0]["status"] == ("pending" if uncertain else "recorded")
    assert calls == ["first"]
    assert step["stop_reconciled"]
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_journal").fetchone()[0] > 0
    await poller.run_due_plan_steps()
    assert await agent.resume_interrupted_turn(step["turn_id"]) is None
    assert calls == ["first"]
    model.ainvoke.assert_awaited_once()


async def test_stop_between_intent_commit_and_dispatch_is_conservative(agent, monkeypatch):
    effect = _tool(agent)
    _model(monkeypatch, [_request()])
    p, s = new_step()
    begin = agent.session_store.begin_external_effect

    async def stop_after_reservation(**kwargs):
        claim = await begin(**kwargs)
        stop(p, "cancelled")
        return claim

    monkeypatch.setattr(agent.session_store, "begin_external_effect", stop_after_reservation)
    await execute(p, s)
    await poller._reconcile_stops()
    effect.assert_not_awaited()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    # A pending intent is uncertainty, not proof that dispatch happened.
    assert (await agent.session_store.list_external_effects(plans.get_step(s)["turn_id"]))[0]["status"] == "pending"


@pytest.mark.parametrize("kind", ["cancelled", "expired"])
async def test_stop_closes_pending_approval_after_owner_release(agent, monkeypatch, kind):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    effect = _tool(agent)
    _model(monkeypatch, [_request()])
    p, s = new_step()
    await execute(p, s)
    step = plans.get_step(s)
    stop(p, kind)
    async with own_conversation(settings.db_path, f"plan:{p}"):
        await poller._reconcile_stops()
        assert not plans.get_step(s)["stop_reconciled"]
        with sqlite3.connect(settings.db_path) as db:
            assert db.execute("SELECT status FROM pending_approvals").fetchone()[0] == "pending"
    await poller._reconcile_stops()
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT status, decided_by FROM pending_approvals").fetchone() == (
            "rejected" if kind == "cancelled" else "expired",
            "plan_lifecycle",
        )
    await agent.resolve_tool_approval(step["approval_id"], approved=True)
    effect.assert_not_awaited()
    assert plans.get_step(s)["state"] == plans.STEP_FAILED


async def test_cancel_after_approval_claim_but_before_dispatch(agent, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    effect = _tool(agent)
    _model(monkeypatch, [_request()])
    p, s = new_step()
    await execute(p, s)
    original = agent.session_store.claim_pending_approval

    async def claim_then_cancel(**kwargs):
        result = await original(**kwargs)
        stop(p, "cancelled")
        return result

    monkeypatch.setattr(agent.session_store, "claim_pending_approval", claim_then_cancel)
    with pytest.raises(ExecutionStoppedError, match="plan_cancelled"):
        await agent.resolve_tool_approval(plans.get_step(s)["approval_id"], approved=True)
    await poller._reconcile_stops()
    effect.assert_not_awaited()
    assert plans.get_step(s)["state"] == plans.STEP_FAILED


@pytest.mark.parametrize("begin", [False, True])
async def test_stopped_claim_link_window_never_creates_replacement_turn(agent, begin):
    p, s = new_step()
    async with own_conversation(settings.db_path, f"plan:{p}") as owner:
        assert plans.claim_step(s, ownership=owner)
        turn = (
            await agent.session_store.begin_turn(
                f"plan:{p}",
                "work",
                caller_key=plans.get_step(s)["execution_key"],
            )
            if begin
            else ""
        )
    stop(p, "cancelled")
    await poller._reconcile_stops()
    step = plans.get_step(s)
    assert step["state"] == plans.STEP_FAILED and step["stop_reconciled"]
    assert step["turn_id"] == turn
    if begin:
        assert len(await agent.session_store.list_turns()) == 1


async def test_completed_result_and_existing_review_survive_stop(agent, monkeypatch):
    _model(monkeypatch, [AIMessage(content="proved result")])
    p, s = new_step()
    await execute(p, s)
    stop(p, "cancelled")
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_DONE
    assert plans.get_step(s)["result"] == "proved result"
    plans._db().write(
        "UPDATE plan_steps SET state='needs_review', result='correlation mismatch', stop_reconciled=0 WHERE id=?", (s,)
    )
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    assert plans.get_step(s)["result"] == "correlation mismatch"


async def test_legacy_unlinked_or_unknown_turn_status_requires_review(agent):
    p, s = new_step()
    assert plans.claim_step(s)
    stop(p, "cancelled")
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    p, s = new_step()
    async with own_conversation(settings.db_path, f"plan:{p}") as owner:
        assert plans.claim_step(s, ownership=owner)
        turn = await agent.session_store.begin_turn(f"plan:{p}", "work", caller_key=plans.get_step(s)["execution_key"])
        plans.link_turn(s, turn, execution_key=plans.get_step(s)["execution_key"])
    with sqlite3.connect(settings.db_path) as db:
        db.execute("UPDATE active_turns SET status='superseded' WHERE turn_id=?", (turn,))
    stop(p, "cancelled")
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW


async def test_stopped_cleanup_requires_current_conversation_capability(agent):
    p, s = new_step()
    stop(p, "cancelled")
    async with own_conversation(settings.db_path, "other") as owner:
        with pytest.raises(DurableStateError, match="ownership"):
            plans.reconcile_stopped_step(
                plans.get_step(s), turn_id="", state=plans.STEP_FAILED, result="stop", ownership=owner
            )


def test_ordinary_failed_plan_never_reports_pending_stop_cleanup():
    p, s = new_step()
    plans._db().write("UPDATE plan_steps SET state='failed' WHERE id=?", (s,))
    plans.settle_plan(p)
    plans._db().write("UPDATE plans SET expires_at=0 WHERE id=?", (p,))
    assert not plans.stop_reason(plans.get_plan(p))
    assert api._plan_view(plans.get_plan(p))["stop_pending_count"] == 0
    assert plans.stopped_steps("kronos") == []


async def test_cancel_during_memory_worker_waits_for_exit_without_model_or_next_write(agent, monkeypatch):
    agent._memory_enabled = True
    entered, release = threading.Event(), threading.Event()
    model = _model(monkeypatch, [AIMessage(content="unused")])

    def retrieve(state):
        entered.set()
        assert release.wait(3)
        return {}

    monkeypatch.setattr("kronos.graph.retrieve_memories", retrieve)
    p, s = new_step()
    task = asyncio.create_task(execute(p, s))
    assert await asyncio.to_thread(entered.wait, 2)
    try:
        stop(p, "cancelled")
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        await poller._reconcile_stops()
        assert not plans.get_step(s)["stop_reconciled"]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_FAILED
    model.ainvoke.assert_not_called()


async def test_cancel_during_memory_persistence_prevents_compaction(agent, monkeypatch):
    agent._memory_enabled = True
    monkeypatch.setattr("kronos.graph.retrieve_memories", lambda state: {})
    _model(monkeypatch, [AIMessage(content="computed but not finalized")])
    compact = AsyncMock()
    monkeypatch.setattr(
        "kronos.graph.get_context_engine", lambda: SimpleNamespace(should_compact=lambda s: True, compact=compact)
    )
    p, s = new_step()

    def store(state):
        stop(p, "cancelled")
        return {}

    monkeypatch.setattr("kronos.graph.store_memories_background", store)
    await execute(p, s)
    await poller._reconcile_stops()
    compact.assert_not_called()
    assert plans.get_step(s)["state"] == plans.STEP_FAILED


async def test_rejected_turn_is_not_promoted_to_done_by_stop_cleanup(agent, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    _tool(agent)
    _model(monkeypatch, [_request(), AIMessage(content="not performed")])
    p, s = new_step()
    await execute(p, s)
    await agent.resolve_tool_approval(plans.get_step(s)["approval_id"], approved=False)
    stop(p, "cancelled")
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_FAILED


def test_stop_migration_preserves_legacy_states_and_does_not_reclassify_failures(tmp_path):
    from kronos.migrations.v006_plan_stop import migrate

    with sqlite3.connect(tmp_path / "legacy.db", isolation_level=None) as db:
        db.executescript("""
            CREATE TABLE plans (id INTEGER PRIMARY KEY, state TEXT);
            CREATE TABLE plan_steps (id INTEGER PRIMARY KEY, plan_id INTEGER, state TEXT, result TEXT);
            INSERT INTO plans VALUES (1, 'cancelled'), (2, 'failed'), (3, 'failed');
            INSERT INTO plan_steps VALUES (1, 1, 'running', ''),
                (2, 2, 'failed', 'plan expired; execution may need reconciliation'),
                (3, 3, 'failed', 'provider unavailable');
        """)
        migrate(db)
        migrate(db)
        assert db.execute("SELECT stop_reason FROM plans ORDER BY id").fetchall() == [
            ("plan_cancelled",),
            ("plan_expired",),
            ("",),
        ]
        assert db.execute("SELECT state, stop_reconciled FROM plan_steps ORDER BY id").fetchall() == [
            ("running", 0),
            ("failed", 0),
            ("failed", 0),
        ]


async def test_stop_uncertainty_survives_crash_between_two_database_commits(agent, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    _tool(agent)
    _model(monkeypatch, [_request()])
    p, s = new_step()
    await execute(p, s)
    turn = plans.get_step(s)["turn_id"]
    # An inconsistent legacy writer claimed completion with an approval pending.
    with sqlite3.connect(settings.db_path) as db:
        db.execute("UPDATE active_turns SET status='done', final_content='unproven' WHERE turn_id=?", (turn,))
    stop(p, "cancelled")
    acknowledge = plans.reconcile_stopped_step
    monkeypatch.setattr(
        plans, "reconcile_stopped_step", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("plan DB unavailable"))
    )
    await poller._reconcile_stops()
    assert not plans.get_step(s)["stop_reconciled"]
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT status FROM pending_approvals").fetchone() == ("rejected",)
        assert db.execute("SELECT error FROM active_turns WHERE turn_id=?", (turn,)).fetchone() == (
            "plan_stop_requires_review",
        )
    monkeypatch.setattr(plans, "reconcile_stopped_step", acknowledge)
    await poller._reconcile_stops()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    assert plans.get_step(s)["stop_reconciled"]


@pytest.mark.parametrize("ephemeral", [False, True])
async def test_plan_named_invocation_cannot_bypass_durable_link(agent, monkeypatch, ephemeral):
    agent._memory_enabled = True
    retrieve = AsyncMock()
    monkeypatch.setattr("kronos.graph.retrieve_memories", retrieve)
    model = _model(monkeypatch, [AIMessage(content="must not run")])
    p, _ = new_step()
    with pytest.raises(DurableStateError):
        await agent.ainvoke_outcome("work", f"plan:{p}", persist_user_turn=not ephemeral)
    model.ainvoke.assert_not_called()
    retrieve.assert_not_called()
