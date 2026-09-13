"""Fault injection at the real SQLite boundary before and after external dispatch."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.agents.supervisor import _make_delegation_tool
from kronos.config import settings
from kronos.effect_state import DurableStateError, EffectUncertainError
from kronos.engine import delegation_ctx, execute_tool, react_loop, side_effect_key
from kronos.session import SessionStore


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = SessionStore(str(tmp_path / "session.db"))
    turn = await store.begin_turn("thread", "send a report")
    return store, turn


def _hooks(store, turn):
    return {
        "turn_id": turn,
        "begin_external_effect": lambda key, name, args, call_id, dedupe_by_key: store.begin_external_effect(
            key=key, turn_id=turn, tool=name, args=args, tool_call_id=call_id, dedupe_by_key=dedupe_by_key
        ),
        "finish_external_effect": lambda key, token, name, result: store.finish_external_effect(
            key=key,
            token=token,
            turn_id=turn,
            tool=name,
            result=result,
        ),
    }


def _tool(effect=None):
    effect = effect or AsyncMock(return_value="sent")

    async def send(text: str) -> str:
        return await effect(text)

    tool = StructuredTool.from_function(coroutine=send, name="send_report", description="test sender")
    return tool, effect


def _call(text="report", call_id="c1"):
    return {"id": call_id, "name": "send_report", "args": {"text": text}}


def _model(*responses):
    model = AsyncMock()
    model.bind_tools = lambda tools: model
    model.ainvoke = AsyncMock(side_effect=responses)
    return model


async def test_intent_is_committed_before_any_effect(runtime):
    store, turn = runtime

    async def effect(text):
        with sqlite3.connect(store.db_path) as db:
            assert db.execute("SELECT status FROM effect_intents").fetchone()[0] == "pending"
            assert db.execute("SELECT COUNT(*) FROM external_effects").fetchone()[0] == 0
        return "sent"

    tool, _ = _tool(effect)
    assert (await execute_tool(tool, _call(), **_hooks(store, turn))).content == "sent"
    assert (await store.list_external_effects(turn))[0]["status"] == "recorded"


async def test_reservation_database_failure_stops_before_dispatch(runtime, monkeypatch):
    store, turn = runtime
    tool, effect = _tool()
    monkeypatch.setattr(store, "begin_external_effect", AsyncMock(side_effect=sqlite3.OperationalError("disk full")))
    with pytest.raises(DurableStateError, match="reserved"):
        await execute_tool(tool, _call(), **_hooks(store, turn))
    effect.assert_not_awaited()


async def test_commit_failure_after_effect_cannot_repeat_even_with_changed_args(runtime, monkeypatch):
    store, turn = runtime
    tool, effect = _tool()
    monkeypatch.setattr(store, "finish_external_effect", AsyncMock(side_effect=sqlite3.OperationalError("disk full")))
    with pytest.raises(EffectUncertainError, match="commit failed"):
        await execute_tool(tool, _call(), **_hooks(store, turn))
    restarted = SessionStore(store.db_path)
    for call in [_call(), _call("different report", "new-id")]:
        with pytest.raises(EffectUncertainError):
            await execute_tool(tool, call, **_hooks(restarted, turn))
    assert effect.await_count == 1
    assert (await restarted.list_external_effects(turn))[0]["status"] == "pending"
    with pytest.raises(EffectUncertainError):
        await restarted.get_external_effect(side_effect_key(tool, _call()["args"], turn))


async def test_lost_commit_acknowledgement_reuses_committed_result(runtime, monkeypatch):
    store, turn = runtime
    tool, effect = _tool()
    original = store.finish_external_effect

    async def committed_then_failed(**kwargs):
        await original(**kwargs)
        raise ConnectionError("ack lost")

    monkeypatch.setattr(store, "finish_external_effect", committed_then_failed)
    with pytest.raises(EffectUncertainError):
        await execute_tool(tool, _call(), **_hooks(store, turn))
    restarted = SessionStore(store.db_path)
    assert (await execute_tool(tool, _call(), **_hooks(restarted, turn))).content == "sent"
    assert effect.await_count == 1


async def test_concurrent_claims_cannot_dispatch_twice(runtime):
    store, turn = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def effect(text):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "sent"

    tool, _ = _tool(effect)
    first = asyncio.create_task(execute_tool(tool, _call(), **_hooks(store, turn)))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(EffectUncertainError):
            await execute_tool(tool, _call(), **_hooks(SessionStore(store.db_path), turn))
    finally:
        release.set()
        await first
    assert calls == 1


async def test_cancel_after_dispatch_keeps_intent_across_restart(runtime):
    store, turn = runtime
    entered = asyncio.Event()
    calls = 0

    async def effect(text):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    tool, _ = _tool(effect)
    task = asyncio.create_task(execute_tool(tool, _call(), **_hooks(store, turn)))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(EffectUncertainError):
        await execute_tool(tool, _call(), **_hooks(SessionStore(store.db_path), turn))
    assert calls == 1


async def test_timeout_after_dispatch_is_uncertain_not_retryable(runtime, monkeypatch):
    store, turn = runtime
    monkeypatch.setattr("kronos.engine.TOOL_TIMEOUT_SECONDS", 0.01)
    effect = AsyncMock(side_effect=lambda text: None)

    async def send(text):
        await effect(text)
        await asyncio.Event().wait()

    tool, _ = _tool(send)
    with pytest.raises(EffectUncertainError, match="timed out"):
        await execute_tool(tool, _call(), **_hooks(store, turn))
    with pytest.raises(EffectUncertainError):
        await execute_tool(tool, _call(), **_hooks(store, turn))
    assert effect.await_count == 1


async def test_pending_intent_is_never_pruned_or_finalized_as_success(runtime):
    store, turn = runtime
    await store.begin_external_effect(key="key", turn_id=turn, tool="send_report")
    for close in [
        store.finish_turn(turn),
        store.finalize_turn(
            turn_id=turn,
            thread_id="thread",
            messages=[AIMessage(content="success")],
            content="success",
        ),
    ]:
        with pytest.raises(EffectUncertainError):
            await close
    await store.fail_turn(turn, "crashed")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET completed_at = datetime('now', '-60 days')")
    assert (await store.prune_turn_history())["turns"] == 0
    assert (await store.get_turn_detail(turn))["effects"][0]["status"] == "pending"


async def test_owner_token_and_legacy_writer_cannot_publish_another_claim(runtime):
    store, turn = runtime
    claim = await store.begin_external_effect(key="key", turn_id=turn, tool="send_report")
    with pytest.raises(DurableStateError, match="claim lost"):
        await store.finish_external_effect(key="key", token="wrong", turn_id=turn, tool="send_report", result="fake")
    assert not await store.record_external_effect(key="key", turn_id=turn, tool="send_report", result="fake")
    await store.finish_external_effect(key="key", token=claim.token, turn_id=turn, tool="send_report", result="real")
    assert await store.get_external_effect("key") == "real"


async def test_missing_or_finished_turn_cannot_start_an_effect(runtime):
    store, turn = runtime
    for invalid in ["missing", turn]:
        if invalid == turn:
            await store.finish_turn(turn)
        with pytest.raises(DurableStateError, match="active durable turn"):
            await store.begin_external_effect(key="key", turn_id=invalid, tool="send_report")


@pytest.mark.parametrize("failing_hook", ["on_message_delta", "get_cached_tool_result"])
async def test_journal_and_cache_read_failures_abort_before_tools(runtime, failing_hook):
    store, turn = runtime
    tool, effect = _tool()
    model = _model(AIMessage(content="", tool_calls=[_call()]))
    hooks = _hooks(store, turn)
    hooks[failing_hook] = AsyncMock(side_effect=sqlite3.OperationalError("unavailable"))
    with pytest.raises(DurableStateError):
        await react_loop(model, [HumanMessage(content="send")], [tool], **hooks)
    effect.assert_not_awaited()


async def test_cache_write_failure_stops_next_effect_but_preserves_first(runtime):
    store, turn = runtime
    tool, effect = _tool()
    model = _model(AIMessage(content="", tool_calls=[_call(), _call("second", "c2")]))
    with pytest.raises(DurableStateError, match="cache write"):
        await react_loop(
            model,
            [HumanMessage(content="send")],
            [tool],
            **_hooks(store, turn),
            save_tool_result=AsyncMock(side_effect=OSError("disk full")),
        )
    assert effect.await_count == 1
    assert (await execute_tool(tool, _call(), **_hooks(store, turn))).content == "sent"
    assert effect.await_count == 1


async def test_custom_delegated_loop_inherits_intent_and_does_not_leak_it(runtime):
    store, turn = runtime
    tool, effect = _tool()

    async def custom_agent(messages):
        # No callback kwargs in this signature, like the built-in Server Ops.
        model = _model(AIMessage(content="", tool_calls=[_call()]), AIMessage(content="child done"))
        return await react_loop(model, messages, [tool])

    delegate = _make_delegation_tool("custom", "test delegation", custom_agent)
    for _ in range(2):
        request = AIMessage(content="", tool_calls=[{"name": delegate.name, "args": {"request": "send"}, "id": "d1"}])
        model = _model(request, AIMessage(content="done"))
        await react_loop(model, [HumanMessage(content="send")], [delegate], **_hooks(store, turn))
        assert delegation_ctx() is None
    assert effect.await_count == 1
    with pytest.raises(DurableStateError, match="intent ledger"):
        await execute_tool(tool, _call())


async def test_nested_uncertainty_is_not_swallowed_as_agent_response(runtime, monkeypatch):
    store, turn = runtime
    tool, effect = _tool()
    monkeypatch.setattr(store, "finish_external_effect", AsyncMock(side_effect=OSError("disk full")))

    async def custom_agent(messages):
        return await react_loop(_model(AIMessage(content="", tool_calls=[_call()])), messages, [tool])

    delegate = _make_delegation_tool("custom", "test delegation", custom_agent)
    model = _model(AIMessage(content="", tool_calls=[{"name": delegate.name, "args": {"request": "send"}, "id": "d1"}]))
    with pytest.raises(EffectUncertainError):
        await react_loop(model, [HumanMessage(content="send")], [delegate], **_hooks(store, turn))
    assert effect.await_count == 1
    assert delegation_ctx() is None


async def test_new_turn_cannot_repeat_same_unresolved_operation(runtime, monkeypatch):
    store, turn = runtime
    tool, effect = _tool()
    monkeypatch.setattr(store, "finish_external_effect", AsyncMock(side_effect=OSError("disk full")))
    with pytest.raises(EffectUncertainError):
        await execute_tool(tool, _call(), **_hooks(store, turn))
    next_turn = await store.begin_turn("another-thread", "try again")
    with pytest.raises(EffectUncertainError):
        await execute_tool(tool, _call(call_id="different-id"), **_hooks(store, next_turn))
    assert effect.await_count == 1
    pending = (await store.list_external_effects(turn))[0]
    assert pending["args"] == {"text": "report"}


async def test_intent_migration_preserves_legacy_results_and_is_concurrent(tmp_path):
    import aiosqlite

    from kronos.migrations.v003_effect_intents import migrate

    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE external_effects (idempotency_key TEXT PRIMARY KEY, result TEXT)")
        db.execute("INSERT INTO external_effects VALUES ('old', 'recorded result')")

    async def upgrade():
        async with aiosqlite.connect(path, timeout=30) as db:
            await migrate(db)

    await asyncio.gather(upgrade(), upgrade(), upgrade())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM external_effects").fetchall() == [("old", "recorded result")]
        assert db.execute("SELECT count(*) FROM effect_intents").fetchone()[0] == 0


async def test_distinct_identical_requests_are_not_silently_collapsed(runtime):
    store, turn = runtime
    tool, effect = _tool()
    await execute_tool(tool, _call(call_id="first-purchase"), **_hooks(store, turn))
    # The second call might be another identical purchase or a regenerated retry.
    # Until operation intent is explicit, stop rather than report both as done.
    with pytest.raises(EffectUncertainError, match="intent review"):
        await execute_tool(tool, _call(call_id="second-purchase"), **_hooks(store, turn))
    assert effect.await_count == 1


async def test_explicit_business_key_allows_regenerated_call_ids(runtime):
    store, turn = runtime
    tool, effect = _tool()
    tool.metadata = {"idempotency_key": lambda args: args["text"]}
    first = await execute_tool(tool, _call(call_id="original"), **_hooks(store, turn))
    second = await execute_tool(tool, _call(call_id="regenerated"), **_hooks(store, turn))
    assert first.content == second.content == "sent"
    assert effect.await_count == 1
