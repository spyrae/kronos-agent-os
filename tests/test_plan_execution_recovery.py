"""Plan recovery must preserve logical execution across both SQLite commits."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from kronos import plans
from kronos.config import settings
from kronos.cron import plans as poller
from kronos.engine import AgentResult
from kronos.graph import KronosAgent
from kronos.session import SessionStore
from kronos.turn_ownership import own_conversation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from kronos import db

    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 0)
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    db._instances.clear()
    obj = object.__new__(KronosAgent)
    obj._session_store = SessionStore(settings.db_path)
    obj._memory_enabled = False
    obj._supervisor = None
    obj._tools = []
    obj._skill_store = None
    obj._system_prompt = "Test agent"
    obj._last_pending_approval_id = None
    obj._last_pending_approval_turn_id = None
    obj._external_tool_event_callback = None

    async def finish(**kwargs):
        return AgentResult([*kwargs["messages"], AIMessage(content="verified result")], "verified result")

    obj._run_model_loop = AsyncMock(side_effect=finish)
    monkeypatch.setattr("kronos.bridge.get_agent", lambda: obj)
    monkeypatch.setattr("kronos.bridge.deliver_plan_approval", AsyncMock(return_value=True))
    monkeypatch.setattr(
        poller,
        "get_policy",
        lambda: SimpleNamespace(durable=SimpleNamespace(resume_mode="resume", max_resume_attempts=2)),
    )
    yield obj
    for instance in db._instances.values():
        instance.conn.close()
    db._instances.clear()


def new_step():
    plan_id = plans.create_plan(agent_name="kronos", goal="recover work")
    return plan_id, plans.add_step(plan_id, "do work")


async def claim(plan_id, step_id, runtime, *, begin=True, link=True):
    async with own_conversation(settings.db_path, f"plan:{plan_id}") as ownership:
        assert plans.claim_step(step_id, ownership=ownership)
        key = plans.get_step(step_id)["execution_key"]
        if not begin:
            return key, None
        turn = await runtime.session_store.begin_turn(f"plan:{plan_id}", "do work", caller_key=key)
        if link:
            plans.link_turn(step_id, turn, execution_key=key)
        return key, turn


async def test_claim_waits_for_ownership_without_marking_step_running(runtime):
    p, s = new_step()
    async with own_conversation(settings.db_path, f"plan:{p}"):
        await poller._run_step(plans.get_plan(p), plans.get_step(s), "")
    assert plans.get_step(s)["state"] == plans.STEP_PENDING
    assert plans.get_step(s)["attempts"] == 0
    runtime._run_model_loop.assert_not_called()


async def test_live_execution_is_not_resumed_even_with_old_timestamp(runtime):
    p, s = new_step()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = runtime._run_model_loop.side_effect

    async def hold(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    runtime._run_model_loop.side_effect = hold
    task = asyncio.create_task(poller._run_step(plans.get_plan(p), plans.get_step(s), ""))
    await entered.wait()
    try:
        plans._db().write("UPDATE plan_steps SET updated_at=0 WHERE id=?", (s,))
        assert await poller._reconcile_turns() == set()
        assert plans.get_step(s)["state"] == plans.STEP_RUNNING
        assert runtime._run_model_loop.await_count == 1
    finally:
        release.set()
        await task
    assert plans.get_step(s)["state"] == plans.STEP_DONE


@pytest.mark.parametrize("link", [False, True])
async def test_restart_reattaches_and_continues_the_same_turn(runtime, link):
    p, s = new_step()
    key, turn = await claim(p, s, runtime, link=link)
    # Generic startup recovery must not consume a caller-owned turn first.
    runtime._session_store = SessionStore(settings.db_path)
    assert await runtime.session_store.recover_abandoned_turns() == 0
    assert await runtime.session_store.resumable_turns() == []
    assert await poller._reconcile_turns() == {p}
    step = plans.get_step(s)
    assert step["state"] == plans.STEP_DONE
    assert step["turn_id"] == turn
    assert (await runtime.session_store.get_turn_for_caller(key))["status"] == "done"
    assert len(await runtime.session_store.list_turns()) == 1
    assert runtime._run_model_loop.await_count == 1


async def test_unstarted_crash_retries_with_backoff_and_the_same_key(runtime):
    p, s = new_step()
    key, _ = await claim(p, s, runtime, begin=False)
    assert await poller._reconcile_turns() == set()
    step = plans.get_step(s)
    assert step["state"] == plans.STEP_PENDING
    assert step["execution_key"] == key
    assert plans.ready_steps("kronos") == []
    runtime._run_model_loop.assert_not_called()
    plans._db().write("UPDATE plan_steps SET wake_at=0 WHERE id=?", (s,))
    await poller._run_step(plans.get_plan(p), plans.get_step(s), "")
    assert plans.get_step(s)["state"] == plans.STEP_DONE
    assert (await runtime.session_store.get_turn_for_caller(key))["status"] == "done"


async def test_late_begin_commit_after_unstarted_retry_cannot_create_second_turn(runtime):
    p, s = new_step()
    key, _ = await claim(p, s, runtime, begin=False)
    await poller._reconcile_turns()
    turn = await runtime.session_store.begin_turn(f"plan:{p}", "late original", caller_key=key)
    plans._db().write("UPDATE plan_steps SET wake_at=0 WHERE id=?", (s,))
    await poller._run_step(plans.get_plan(p), plans.get_step(s), "")
    runtime._run_model_loop.assert_not_called()
    assert plans.get_step(s)["turn_id"] == turn
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_DONE
    assert len(await runtime.session_store.list_turns()) == 1


async def test_unstarted_attempts_are_bounded(runtime):
    p, s = new_step()
    await claim(p, s, runtime, begin=False)
    plans._db().write("UPDATE plan_steps SET attempts=? WHERE id=?", (plans.MAX_STEP_ATTEMPTS, s))
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_FAILED
    runtime._run_model_loop.assert_not_called()


async def test_legacy_unlinked_execution_is_review_not_a_fresh_attempt(runtime):
    p, s = new_step()
    assert plans.claim_step(s)
    plans.add_step(p, "another operation")
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    assert plans.ready_steps("kronos") == []
    runtime._run_model_loop.assert_not_called()


async def test_report_policy_preserves_manual_resume(runtime, monkeypatch):
    p, s = new_step()
    _, turn = await claim(p, s, runtime)
    monkeypatch.setattr(poller, "get_policy", lambda: SimpleNamespace(durable=SimpleNamespace(resume_mode="report")))
    assert await poller._reconcile_turns() == set()
    assert plans.get_step(s)["state"] == plans.STEP_INTERRUPTED
    assert plans.ready_steps("kronos") == []
    assert plans.plan_for_turn(turn, "kronos")["id"] == p
    runtime._run_model_loop.assert_not_called()
    assert await runtime.resume_interrupted_turn(turn) == "verified result"
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_DONE


async def test_cancelled_invocation_is_recovered_only_after_it_unwinds(runtime):
    p, s = new_step()
    entered = asyncio.Event()
    original = runtime._run_model_loop.side_effect

    async def hold(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    runtime._run_model_loop.side_effect = hold
    task = asyncio.create_task(poller._run_step(plans.get_plan(p), plans.get_step(s), ""))
    await entered.wait()
    turn = plans.get_step(s)["turn_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime._run_model_loop.side_effect = original
    await poller._reconcile_turns()
    assert plans.get_step(s)["turn_id"] == turn
    assert plans.get_step(s)["state"] == plans.STEP_DONE


async def test_pending_intent_goes_to_review_without_model_or_replacement(runtime):
    p, s = new_step()
    _, turn = await claim(p, s, runtime)
    await runtime.session_store.begin_external_effect(
        key="uncertain", turn_id=turn, tool="send", args={}, tool_call_id="call"
    )
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    runtime._run_model_loop.assert_not_called()
    assert len(await runtime.session_store.list_turns()) == 1


async def test_recovery_and_fresh_steps_share_cycle_budget(runtime):
    recovery = []
    for _ in range(poller.MAX_STEPS_PER_CYCLE + 1):
        p, s = new_step()
        await claim(p, s, runtime)
        recovery.append(s)
    _, fresh = new_step()
    # Summaries have their own quota; isolate the execution budget under test.
    from unittest.mock import patch

    with patch.object(poller, "_deliver_pending_summaries", AsyncMock(return_value=0)):
        await poller.run_due_plan_steps()
    assert runtime._run_model_loop.await_count == poller.MAX_STEPS_PER_CYCLE
    assert sum(plans.get_step(s)["state"] == plans.STEP_DONE for s in recovery) == poller.MAX_STEPS_PER_CYCLE
    assert plans.get_step(fresh)["state"] == plans.STEP_PENDING


async def test_recovered_plan_does_not_run_next_step_in_same_cycle(runtime, monkeypatch):
    p, s = new_step()
    next_step = plans.add_step(p, "next", depends_on=[s])
    await claim(p, s, runtime)
    monkeypatch.setattr(poller, "_deliver_pending_summaries", AsyncMock(return_value=0))
    await poller.run_due_plan_steps()
    assert plans.get_step(s)["state"] == plans.STEP_DONE
    assert plans.get_step(next_step)["state"] == plans.STEP_PENDING


@pytest.mark.parametrize("legacy", [False, True])
async def test_park_survives_completion_and_can_be_released(runtime, legacy):
    p, s = new_step()
    _, turn = await claim(p, s, runtime)
    plans.park_step(s, {"kind": "manual"})
    assert plans.get_step(s)["state"] == plans.STEP_RUNNING
    if legacy:
        plans._db().write("UPDATE plan_steps SET state='waiting', repark_requested=0 WHERE id=?", (s,))
    assert not plans.release_step(s)
    await poller._reconcile_turns()
    step = plans.get_step(s)
    assert step["state"] == plans.STEP_WAITING
    assert step["turn_id"] == "" and step["last_turn_id"] == turn
    assert step["execution_key"] == "" and not step["repark_requested"]
    assert plans.release_step(s)
    await poller._run_step(plans.get_plan(p), plans.get_step(s), "")
    assert plans.get_step(s)["state"] == plans.STEP_DONE
    assert plans.get_step(s)["turn_id"] != turn


async def test_legacy_park_is_preserved_through_interrupted_state(runtime, monkeypatch):
    p, s = new_step()
    _, turn = await claim(p, s, runtime)
    plans._db().write("UPDATE plan_steps SET state='waiting', wait_json=? WHERE id=?", ('{"kind":"manual"}', s))
    monkeypatch.setattr(poller, "get_policy", lambda: SimpleNamespace(durable=SimpleNamespace(resume_mode="report")))
    await poller._reconcile_turns()
    assert plans.get_step(s)["repark_requested"] == 1
    await runtime.resume_interrupted_turn(turn)
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_WAITING


async def test_cancelled_plan_cannot_be_reparked_or_released(runtime):
    p, s = new_step()
    plans.park_step(s, {"kind": "manual"})
    plans.cancel_plan(p, "kronos")
    assert not plans.release_step(s)
    with pytest.raises(plans.PlanError):
        plans.park_step(s, {"kind": "manual"})


async def test_corrupt_condition_requires_review_without_dispatch(runtime):
    p, s = new_step()
    plans._db().write("UPDATE plan_steps SET state='waiting', wait_json='{' WHERE id=?", (s,))
    await poller.run_due_plan_steps()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    runtime._run_model_loop.assert_not_called()


async def test_caller_identity_is_unique_and_not_lost_to_retention(runtime):
    p, s = new_step()
    key, turn = await claim(p, s, runtime, link=False)
    with pytest.raises(sqlite3.IntegrityError):
        await runtime.session_store.begin_turn(f"plan:{p}", "duplicate", caller_key=key)
    await runtime.session_store.fail_turn(turn, "after external work")
    with sqlite3.connect(settings.db_path) as db:
        db.execute("UPDATE active_turns SET completed_at='2000-01-01' WHERE turn_id=?", (turn,))
    assert (await runtime.session_store.prune_turn_history())["turns"] == 0
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    runtime._run_model_loop.assert_not_called()


async def test_wrong_thread_correlation_cannot_authorize_execution(runtime):
    p, s = new_step()
    key, _ = await claim(p, s, runtime, begin=False)
    await runtime.session_store.begin_turn("unrelated", "wrong", caller_key=key)
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_REVIEW
    runtime._run_model_loop.assert_not_called()


async def test_stale_reconciliation_snapshot_does_not_reclassify_completed_park(runtime, monkeypatch):
    p, s = new_step()
    _, turn = await claim(p, s, runtime)
    stale = plans.get_step(s)
    plans.park_step(s, {"kind": "manual"})
    assert plans.complete_step_turn(s, turn, "partial result")
    monkeypatch.setattr(plans, "steps_to_reconcile", lambda *a: [stale])
    await poller._reconcile_turns()
    assert plans.get_step(s)["state"] == plans.STEP_WAITING
    runtime._run_model_loop.assert_not_called()


async def test_stale_condition_result_cannot_repark_a_released_or_live_step(runtime):
    p, s = new_step()
    plans.park_step(s, {"kind": "manual"})
    assert plans.release_step(s)
    assert not plans.note_check(s, 9999999999)
    assert plans.get_step(s)["state"] == plans.STEP_PENDING
    await claim(p, s, runtime)
    assert not plans.note_check(s, 9999999999)
    assert plans.get_step(s)["state"] == plans.STEP_RUNNING


async def test_pending_approval_is_not_resumed_as_an_abandoned_model(runtime):
    p, s = new_step()
    _, turn = await claim(p, s, runtime)
    await runtime.session_store.create_pending_approval(
        turn_id=turn,
        thread_id=f"plan:{p}",
        tool_call_id="call",
        tool_name="send",
        args={},
    )
    assert await poller._reconcile_turns() == set()
    assert plans.get_step(s)["state"] == plans.STEP_APPROVAL
    runtime._run_model_loop.assert_not_called()


def test_migration_preserves_legacy_claims_and_is_idempotent(tmp_path):
    from kronos.migrations.v005_plan_execution import migrate_plans

    with sqlite3.connect(tmp_path / "old.db") as db:
        db.execute("CREATE TABLE plan_steps (id INTEGER, state TEXT)")
        db.execute("INSERT INTO plan_steps VALUES (1, 'running')")
        db.commit()
        migrate_plans(db)
        migrate_plans(db)
        row = db.execute("SELECT state, execution_key, last_turn_id, repark_requested FROM plan_steps").fetchone()
    assert row == ("running", "", "", 0)


async def test_wrong_claim_key_cannot_attach_a_turn(runtime):
    p, s = new_step()
    await claim(p, s, runtime, begin=False)
    with pytest.raises(plans.PlanError):
        plans.link_turn(s, "foreign", execution_key="different-key")
    assert plans.get_step(s)["turn_id"] == ""


async def test_park_cannot_break_the_claim_before_turn_link(runtime):
    p, s = new_step()
    key, _ = await claim(p, s, runtime, begin=False)
    with pytest.raises(plans.PlanError):
        plans.park_step(s, {"kind": "manual"})
    assert plans.get_step(s)["state"] == plans.STEP_RUNNING
    assert plans.get_step(s)["execution_key"] == key
