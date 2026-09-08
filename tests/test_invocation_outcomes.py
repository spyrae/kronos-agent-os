"""Execution evidence must survive approval pauses, compaction and restarts."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.engine import AgentResult, react_loop
from kronos.graph import KronosAgent
from kronos.migrations.v001_turn_outcome import migrate
from kronos.session import SessionStore


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    obj = object.__new__(KronosAgent)
    obj._session_store = SessionStore(str(tmp_path / "session.db"))
    obj._memory_enabled = False
    obj._supervisor = None
    obj._tools = []
    obj._skill_store = None
    obj._system_prompt = "system"
    obj._external_tool_event_callback = None
    obj._durable_recovery_checked = True
    obj._last_pending_approval_id = None
    import kronos.db as db_module

    db_module._instances.clear()
    yield obj
    db_module._instances.clear()


def _model(monkeypatch, responses):
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=responses)
    model.bind_tools = lambda tools: model
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
    return model


def _request(call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": "restart_service", "args": {}, "id": call_id}])


def _tool(agent):
    effect = AsyncMock(return_value="restarted")

    async def restart() -> str:
        return await effect()

    agent._tools = [
        StructuredTool.from_function(
            coroutine=restart,
            name="restart_service",
            description="test-only counter",
        )
    ]
    return effect


async def test_exact_outcome_is_retained_not_reconstructed_from_history(agent, monkeypatch):
    _model(monkeypatch, [AIMessage(content="first"), AIMessage(content="second")])
    first = await agent.ainvoke_outcome("hello", "thread")
    assert first.status == "completed"
    assert first.content == "first"
    assert await agent.ainvoke("next", "thread") == "second"
    await agent._session_store.clear("thread")
    restarted = SessionStore(agent._session_store.db_path)
    assert await restarted.get_turn_outcome(first.turn_id) == first
    with sqlite3.connect(restarted.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_journal").fetchone()[0] == 0


async def test_turn_link_is_persisted_before_model_and_failure_prevents_effects(agent):
    links = []

    async def run(**kwargs):
        assert links
        assert (await agent.get_turn_outcome(links[0])).status == "running"
        return AgentResult(messages=[], content="done")

    agent._run_model_loop = AsyncMock(side_effect=run)
    result = await agent.ainvoke_outcome("hello", "plan:1", on_turn_started=links.append)
    assert links == [result.turn_id]

    def broken_link(turn_id):
        links.append(turn_id)
        raise RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await agent.ainvoke_outcome("next", "plan:2", on_turn_started=broken_link)
    assert agent._run_model_loop.await_count == 1
    assert (await agent.get_turn_outcome(links[1])).status == "failed"


@pytest.mark.parametrize("ephemeral", [True, False])
async def test_link_requires_persistence(agent, ephemeral):
    if not ephemeral:
        agent._session_store = None
    with pytest.raises(ValueError, match="durable session store"):
        await agent.ainvoke_outcome(
            "hi", "thread", persist_user_turn=not ephemeral, on_turn_started=lambda turn_id: None
        )


async def test_validation_rejection_is_not_completion(agent, monkeypatch):
    monkeypatch.setattr("kronos.graph.validate_input", lambda *args, **kwargs: "blocked input")
    result = await agent.ainvoke_outcome("bad", "thread")
    assert result.status == "blocked"
    assert result.turn_id is None
    assert result.content == "blocked input"


@pytest.mark.parametrize("approved", [True, False])
async def test_approval_continuation_is_correlated_after_store_restart(agent, monkeypatch, approved):
    effects = _tool(agent)
    _model(monkeypatch, [_request(), AIMessage(content="final continuation")])
    paused = await agent.ainvoke_outcome("do it", "thread42")
    assert paused.status == "waiting_approval"
    assert paused.approval_id
    effects.assert_not_awaited()
    agent._session_store = SessionStore(agent._session_store.db_path)
    waiting = await agent.get_turn_outcome(paused.turn_id)
    assert waiting.status == "waiting_approval"
    assert waiting.approval_id == paused.approval_id
    assert waiting.thread_id == "thread42"
    await agent.resolve_tool_approval(paused.approval_id, approved=approved)
    result = await agent.get_turn_outcome(paused.turn_id)
    assert result.status == ("completed" if approved else "rejected")
    assert result.content == "final continuation"
    assert effects.await_count == int(approved)
    await agent.resolve_tool_approval(paused.approval_id, approved=True)
    assert effects.await_count == int(approved)


async def test_concurrent_calls_return_their_own_approval_not_mutable_last_id(agent):
    arrived = 0
    gate = asyncio.Event()

    async def pause(**kwargs):
        nonlocal arrived
        approval = await kwargs["react_loop_kwargs"]["request_tool_approval"](
            type("Tool", (), {"name": "restart_service"})(),
            {"id": "c1", "args": {}},
        )
        arrived += 1
        if arrived == 2:
            gate.set()
        await gate.wait()
        return AgentResult([], "need approval", waiting_approval=True, approval_id=approval)

    agent._run_model_loop = pause
    one, two = await asyncio.gather(
        agent.ainvoke_outcome("first", "plan:1"),
        agent.ainvoke_outcome("second", "plan:2"),
    )
    assert one.approval_id != two.approval_id
    assert one.turn_id != two.turn_id
    for outcome in (one, two):
        saved = await agent.get_turn_outcome(outcome.turn_id)
        assert saved.approval_id == outcome.approval_id
        assert saved.thread_id == outcome.thread_id


async def test_claimed_decision_is_not_terminal_while_continuation_runs(agent, monkeypatch):
    _tool(agent)
    _model(monkeypatch, [_request()])
    paused = await agent.ainvoke_outcome("do it", "plan:1")
    await agent._session_store.claim_pending_approval(approval_id=paused.approval_id, decision="rejected")
    assert (await agent.get_turn_outcome(paused.turn_id)).status == "running"


async def test_expired_approval_never_resumes_and_is_not_pruned(agent, monkeypatch):
    effects = _tool(agent)
    _model(monkeypatch, [_request()])
    paused = await agent.ainvoke_outcome("do it", "plan:1")
    with sqlite3.connect(agent._session_store.db_path) as db:
        db.execute("UPDATE pending_approvals SET requested_at = datetime('now', '-40 days')")
        db.execute("UPDATE active_turns SET started_at = datetime('now', '-40 days')")
    assert (await agent.get_turn_outcome(paused.turn_id)).status == "expired"
    assert (await agent._session_store.prune_turn_history())["turns"] == 0
    await agent.resolve_tool_approval(paused.approval_id, approved=True)
    assert (await agent.get_turn_outcome(paused.turn_id)).status == "expired"
    effects.assert_not_awaited()


async def test_model_error_is_failed_even_with_nonempty_response(agent, monkeypatch):
    _model(monkeypatch, [RuntimeError("provider offline")])
    result = await agent.ainvoke_outcome("hello", "plan:1")
    assert result.content
    assert result.status == "failed"
    assert result.reason == "model_error"
    assert await agent.get_turn_outcome(result.turn_id) == result


async def test_iteration_limit_is_not_completed(monkeypatch):
    model = _model(monkeypatch, [])
    result = await react_loop(model=model, messages=[HumanMessage(content="hello")], tools=[], max_turns=0)
    assert result.content
    assert result.failure_reason == "iteration_limit"


async def test_loop_breaker_is_not_completed(monkeypatch):
    from kronos.security.loop_detector import LoopLevel

    model = _model(monkeypatch, [AIMessage(content="", tool_calls=[{"name": "missing", "id": "c1", "args": {}}])])
    monkeypatch.setattr("kronos.engine.LoopDetector.check", lambda self: (LoopLevel.CIRCUIT_BREAKER, "stuck"))
    result = await react_loop(model=model, messages=[HumanMessage(content="hello")], tools=[])
    assert result.failure_reason == "loop_circuit_breaker"


async def test_legacy_unknown_missing_and_failed_turns_are_not_success(agent):
    store = agent._session_store
    turn = await store.begin_turn("thread", "old question")
    await store.finish_turn(turn)
    result = await store.get_turn_outcome(turn)
    assert result.status == "unknown"
    assert result.reason == "terminal_content_missing"
    assert (await store.get_turn_outcome("nonexistent")).status == "unknown"
    failed = await store.begin_turn("thread", "failed question")
    await store.fail_turn(failed, "provider error")
    assert (await store.get_turn_outcome(failed)).status == "failed"


async def test_migration_retains_legacy_rows_and_is_concurrent_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE active_turns (turn_id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO active_turns VALUES ('old', 'done')")

    async def upgrade():
        async with aiosqlite.connect(path, timeout=30) as db:
            await migrate(db)

    await asyncio.gather(upgrade(), upgrade(), upgrade())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM active_turns").fetchall() == [("old", "done", None)]
        assert sum(row[1] == "final_content" for row in db.execute("PRAGMA table_info(active_turns)")) == 1


@pytest.fixture
def plan_bridge(agent, monkeypatch):
    from types import SimpleNamespace

    from kronos import bridge
    from kronos.cron import plans as poller

    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "tg_bot_token", "")
    monkeypatch.setattr(settings, "allowed_users", "77")
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=10)))
    monkeypatch.setattr(bridge, "_agent", agent)
    monkeypatch.setattr(bridge, "_client", client)
    monkeypatch.setattr(bridge, "_rate_limit_wait", AsyncMock())
    monkeypatch.setattr(poller, "send_webhook", lambda *args, **kwargs: True)
    return client


def _plan_step():
    from kronos import plans

    plan_id = plans.create_plan(agent_name="kronos", goal="test goal", chat_id=77)
    step_id = plans.add_step(plan_id, "test operation")
    return plan_id, step_id


async def test_plan_waits_until_approved_continuation_and_never_reinvokes(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    effects = _tool(agent)
    model = _model(monkeypatch, [_request(), AIMessage(content="operation completed"), AIMessage(content="summary")])
    plan_id, step_id = _plan_step()
    dependent = plans.add_step(plan_id, "must wait", depends_on=[step_id])
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    assert step["state"] == plans.STEP_APPROVAL
    assert step["turn_id"] and step["approval_id"]
    assert plan_bridge.send_message.await_count == 1
    prompt = plan_bridge.send_message.call_args.args[1]
    assert f"/approve {step['approval_id']}" in prompt
    assert "buttons" not in plan_bridge.send_message.call_args.kwargs, "userbots need commands"
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_ACTIVE
    assert plans.get_step(dependent)["attempts"] == 0
    await poller.run_due_plan_steps()
    assert plan_bridge.send_message.await_count == 1
    assert model.ainvoke.await_count == 1
    effects.assert_not_awaited()

    # A restarted store sees the same approval, not a new invocation.
    agent._session_store = SessionStore(agent._session_store.db_path)
    await agent.resolve_tool_approval(step["approval_id"], approved=True, decided_by="77")
    await poller.run_due_plan_steps()
    assert plans.get_step(step_id)["state"] == plans.STEP_DONE
    assert plans.get_step(step_id)["result"] == "operation completed"
    assert effects.await_count == 1
    assert plans.get_step(dependent)["attempts"] == 1


@pytest.mark.parametrize("decision", ["rejected", "expired"])
async def test_plan_refusal_is_terminal_not_automatic_reapproval(agent, plan_bridge, monkeypatch, decision):
    from kronos import plans
    from kronos.cron import plans as poller

    effects = _tool(agent)
    _model(monkeypatch, [_request(), AIMessage(content="not performed"), AIMessage(content="summary")])
    plan_id, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    if decision == "rejected":
        await agent.resolve_tool_approval(step["approval_id"], approved=False)
    else:
        with sqlite3.connect(agent._session_store.db_path) as db:
            db.execute("UPDATE pending_approvals SET requested_at = datetime('now', '-2 hours')")
    await poller.run_due_plan_steps()
    assert plans.get_step(step_id)["state"] == plans.STEP_FAILED
    assert plans.get_step(step_id)["attempts"] == 1
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_FAILED
    assert decision in plans.get_step(step_id)["result"]
    await poller.run_due_plan_steps()
    effects.assert_not_awaited()


async def test_unsent_approval_retries_delivery_not_execution(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    _tool(agent)
    model = _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    plan_bridge.send_message.side_effect = [RuntimeError("offline"), object()]
    await poller.run_due_plan_steps()
    assert plans.get_step(step_id)["notified_approval_id"] == ""
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    assert step["notified_approval_id"] == step["approval_id"]
    assert plan_bridge.send_message.await_count == 2
    assert model.ainvoke.await_count == 1


async def test_crash_after_approval_before_poller_save_is_reconciled(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    _tool(agent)
    model = _model(monkeypatch, [_request()])
    plan_id, step_id = _plan_step()
    assert plans.claim_step(step_id)
    await agent.ainvoke_outcome(
        "operation", f"plan:{plan_id}", on_turn_started=lambda turn_id: plans.link_turn(step_id, turn_id)
    )
    assert plans.get_step(step_id)["state"] == plans.STEP_RUNNING
    agent._session_store = SessionStore(agent._session_store.db_path)
    await poller.run_due_plan_steps()
    assert plans.get_step(step_id)["state"] == plans.STEP_APPROVAL
    assert model.ainvoke.await_count == 1
    assert plan_bridge.send_message.await_count == 1


async def test_cancelled_plan_cannot_execute_pending_approval_or_resume(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    effects = _tool(agent)
    _model(monkeypatch, [_request()])
    plan_id, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    plans.cancel_plan(plan_id, "kronos")
    await agent.resolve_tool_approval(step["approval_id"], approved=True)
    effects.assert_not_awaited()
    assert not plans.update_linked_step(step_id, step["turn_id"], state=plans.STEP_DONE, result="wrong")
    assert await agent.resume_interrupted_turn({"turn_id": step["turn_id"], "thread_id": f"plan:{plan_id}"}) is None
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_CANCELLED
    effects.assert_not_awaited()


async def test_concurrent_pollers_claim_only_one_plan_turn(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    _tool(agent)
    model = _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    await asyncio.gather(poller.run_due_plan_steps(), poller.run_due_plan_steps())
    assert plans.get_step(step_id)["attempts"] == 1
    assert model.ainvoke.await_count == 1


@pytest.mark.parametrize("sender_id,chat_id", [(78, 77), (77, 88)])
async def test_plan_approval_command_rejects_wrong_user_or_destination(
    agent,
    plan_bridge,
    monkeypatch,
    sender_id,
    chat_id,
):
    from types import SimpleNamespace

    from kronos import plans
    from kronos.bridge_plan_approval import handle_plan_approval_command
    from kronos.cron import plans as poller

    effects = _tool(agent)
    _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    event = SimpleNamespace(
        raw_text=f"/approve {step['approval_id']}",
        sender_id=sender_id,
        chat_id=chat_id,
        is_private=True,
        respond=AsyncMock(),
    )
    assert await handle_plan_approval_command(event)
    effects.assert_not_awaited()
    event.respond.assert_not_awaited()
    assert (await agent.get_turn_outcome(step["turn_id"])).status == "waiting_approval"


async def test_owner_command_completes_real_plan_turn(agent, plan_bridge, monkeypatch):
    from types import SimpleNamespace

    from kronos import plans
    from kronos.bridge_plan_approval import handle_plan_approval_command
    from kronos.cron import plans as poller

    effects = _tool(agent)
    _model(monkeypatch, [_request(), AIMessage(content="completed"), AIMessage(content="summary")])
    plan_id, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    event = SimpleNamespace(
        raw_text=f"/approve {step['approval_id']}", sender_id=77, chat_id=77, is_private=True, respond=AsyncMock()
    )
    assert await handle_plan_approval_command(event)
    assert effects.await_count == 1
    await poller.run_due_plan_steps()
    assert plans.get_step(step_id)["result"] == "completed"
    assert plans.get_plan(plan_id)["state"] == plans.PLAN_DONE


async def test_failed_turn_with_nonempty_answer_requires_review_not_rerun(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    model = _model(monkeypatch, [RuntimeError("provider unavailable")])
    _, step_id = _plan_step()
    await poller.run_due_plan_steps()
    assert plans.get_step(step_id)["state"] == plans.STEP_REVIEW
    await poller.run_due_plan_steps()
    assert model.ainvoke.await_count == 1


def test_plan_migration_retains_legacy_rows_and_is_repeatable(tmp_path):
    from kronos.migrations.v002_plan_turns import migrate as migrate_plans

    with sqlite3.connect(tmp_path / "plans.db", isolation_level=None) as db:
        db.execute("CREATE TABLE plan_steps (id INTEGER PRIMARY KEY, state TEXT)")
        db.execute("INSERT INTO plan_steps VALUES (1, 'running')")
        migrate_plans(db)
        migrate_plans(db)
        assert db.execute("SELECT * FROM plan_steps").fetchall() == [(1, "running", "", "", "")]


async def test_next_approval_notifies_once_and_still_blocks_plan(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    effects = _tool(agent)
    # The second request has a different call id and arguments/tool scope would
    # normally matter; create a second gate directly to exercise reconciliation.
    _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    store = agent._session_store
    await store.claim_pending_approval(approval_id=step["approval_id"], decision="approved")
    second_id = await store.create_pending_approval(
        turn_id=step["turn_id"],
        thread_id=f"plan:{step['plan_id']}",
        tool_call_id="second",
        tool_name="restart_service",
        args={"target": "other"},
    )
    await poller.run_due_plan_steps()
    await poller.run_due_plan_steps()
    current = plans.get_step(step_id)
    assert current["state"] == plans.STEP_APPROVAL
    assert current["notified_approval_id"] == second_id
    assert plan_bridge.send_message.await_count == 2
    effects.assert_not_awaited()


async def test_plan_callback_requires_owner_and_exact_topic(agent, plan_bridge, monkeypatch):
    from types import SimpleNamespace

    from kronos import bridge, plans
    from kronos.cron import plans as poller

    _tool(agent)
    _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    pending = await agent.get_pending_tool_approval(step["approval_id"])
    event = SimpleNamespace(chat_id=77, is_private=True)
    assert await bridge._approval_callback_allowed(event, sender_id=77, pending=pending)
    assert not await bridge._approval_callback_allowed(event, sender_id=78, pending=pending)
    event.chat_id = 99
    assert not await bridge._approval_callback_allowed(event, sender_id=77, pending=pending)
    event.chat_id = 77
    monkeypatch.setattr(bridge, "_approval_callback_topic_id", AsyncMock(return_value=123))
    assert not await bridge._approval_callback_allowed(event, sender_id=77, pending=pending)


async def test_allow_all_chat_users_does_not_allow_plan_approval(agent, plan_bridge, monkeypatch):
    from types import SimpleNamespace

    from kronos import bridge, plans
    from kronos.bridge_plan_approval import handle_plan_approval_command
    from kronos.cron import plans as poller

    effects = _tool(agent)
    _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    await poller.run_due_plan_steps()
    step = plans.get_step(step_id)
    monkeypatch.setattr(settings, "allowed_users", "")
    monkeypatch.setattr(settings, "allow_all_users", True)
    event = SimpleNamespace(
        raw_text=f"/approve {step['approval_id']}", sender_id=77, chat_id=77, is_private=True, respond=AsyncMock()
    )
    assert await handle_plan_approval_command(event)
    pending = await agent.get_pending_tool_approval(step["approval_id"])
    assert not await bridge._approval_callback_allowed(event, sender_id=77, pending=pending)
    effects.assert_not_awaited()


async def test_bot_plan_prompt_has_matching_inline_callback(agent, plan_bridge, monkeypatch):
    from kronos import plans
    from kronos.cron import plans as poller

    monkeypatch.setattr(settings, "tg_bot_token", "test-token-not-real")
    _tool(agent)
    _model(monkeypatch, [_request()])
    _, step_id = _plan_step()
    await poller.run_due_plan_steps()
    approval_id = plans.get_step(step_id)["approval_id"]
    buttons = plan_bridge.send_message.call_args.kwargs["buttons"]
    assert buttons[0][0].data == f"kaos:approval:approve:{approval_id}".encode()
    assert buttons[0][1].data == f"kaos:approval:reject:{approval_id}".encode()


async def test_concurrent_cold_stores_initialize_and_keep_every_turn(tmp_path):
    for index in range(10):
        path = str(tmp_path / f"cold-{index}.db")
        stores = [SessionStore(path) for _ in range(4)]
        turns = await asyncio.gather(*(store.begin_turn(f"thread:{i}", "hello") for i, store in enumerate(stores)))
        assert len(set(turns)) == 4
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM active_turns").fetchone()[0] == 4
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
