"""Execution scopes reach nested loops, custom calls and worker threads."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.engine import execute_tool, react_loop
from kronos.execution_control import (
    ExecutionStoppedError,
    check_execution,
    execution_scope,
    guard_model,
    run_sync_owned,
)
from kronos.llm import FallbackChatModel


def guard(state):
    def check():
        if state["stopped"]:
            raise ExecutionStoppedError("stopped")

    return check


async def test_scopes_compose_and_do_not_contaminate_independent_tasks():
    outer, inner = {"stopped": False}, {"stopped": False}
    entered, release = asyncio.Event(), asyncio.Event()

    async def stopped_task():
        with execution_scope(guard(outer)):
            entered.set()
            await release.wait()
            with execution_scope(guard(inner)):
                with pytest.raises(ExecutionStoppedError):
                    check_execution()
        check_execution()

    task = asyncio.create_task(stopped_task())
    await entered.wait()
    outer["stopped"] = True
    check_execution()
    release.set()
    await task
    with execution_scope(guard(inner)):
        check_execution()


@pytest.mark.parametrize("sync", [False, True])
async def test_checked_model_stops_after_inflight_call_before_followup(sync):
    state = {"stopped": False}

    def response(*args):
        state["stopped"] = True
        return AIMessage(content="finished in flight")

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = response
    model.ainvoke = AsyncMock(side_effect=response)
    with execution_scope(guard(state)):
        checked = guard_model(model).bind_tools([])
        with pytest.raises(ExecutionStoppedError):
            if sync:
                checked.invoke([])
            else:
                await checked.ainvoke([])
        with pytest.raises(ExecutionStoppedError):
            if sync:
                checked.invoke([])
            else:
                await checked.ainvoke([])
    assert model.invoke.call_count + model.ainvoke.await_count == 1
    assert guard_model(model) is model


@pytest.mark.parametrize("sync", [False, True])
async def test_cancel_prevents_fallback_to_next_provider(monkeypatch, sync):
    state = {"stopped": False}

    def fail(*args):
        state["stopped"] = True
        raise TimeoutError("provider failed")

    first = Mock(invoke=Mock(side_effect=fail), ainvoke=AsyncMock(side_effect=fail))
    fallback = FallbackChatModel(["first", "second"], "test")
    monkeypatch.setattr(fallback, "_providers_for_attempt", lambda: ["first", "second"])
    prepare = Mock(return_value=first)
    monkeypatch.setattr(fallback, "_prepare_model", prepare)
    monkeypatch.setattr("kronos.llm._state.mark_failed", lambda provider: None)
    with execution_scope(guard(state)), pytest.raises(ExecutionStoppedError):
        if sync:
            fallback.invoke([])
        else:
            await fallback.ainvoke([])
    assert prepare.call_args_list == [(("first",), {})]


async def test_nested_react_loop_inherits_stop_from_outer_tool():
    state = {"stopped": False}
    child_model = AsyncMock()
    child_model.bind_tools = lambda tools: child_model

    async def delegate() -> str:
        state["stopped"] = True
        return (await react_loop(model=child_model, messages=[HumanMessage(content="child")], tools=[])).content

    tool = StructuredTool.from_function(coroutine=delegate, name="delegate_test", description="test delegation")
    with execution_scope(guard(state)), pytest.raises(ExecutionStoppedError):
        await execute_tool(tool, {"id": "nested", "args": {}})
    child_model.ainvoke.assert_not_awaited()


async def test_direct_topic_tool_and_knowledge_save_check_scope(monkeypatch):
    from kronos.agents.knowledge_pipeline import queue
    from kronos.agents.topic_research.nodes.discover import _audited_tool_call

    state = {"stopped": True}
    tool = Mock(name="reader", ainvoke=AsyncMock())
    with execution_scope(guard(state)), pytest.raises(ExecutionStoppedError):
        await _audited_tool_call(tool, {})
    tool.ainvoke.assert_not_awaited()
    with execution_scope(guard(state)), pytest.raises(ExecutionStoppedError):
        object.__new__(queue.KnowledgeQueue).save_task({})


async def test_sync_worker_receives_scope_and_checks_before_dispatch():
    state = {"stopped": False}

    def worker():
        state["stopped"] = True
        check_execution()

    with execution_scope(guard(state)), pytest.raises(ExecutionStoppedError):
        await run_sync_owned(worker)
    called = Mock()
    with execution_scope(guard(state)), pytest.raises(ExecutionStoppedError):
        await run_sync_owned(called)
    called.assert_not_called()
