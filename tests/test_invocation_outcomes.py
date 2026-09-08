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
    return obj


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
    paused = await agent.ainvoke_outcome("do it", "plan:42")
    assert paused.status == "waiting_approval"
    assert paused.approval_id
    effects.assert_not_awaited()
    agent._session_store = SessionStore(agent._session_store.db_path)
    waiting = await agent.get_turn_outcome(paused.turn_id)
    assert waiting.status == "waiting_approval"
    assert waiting.approval_id == paused.approval_id
    assert waiting.thread_id == "plan:42"
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
