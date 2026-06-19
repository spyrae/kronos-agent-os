import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from kronos.engine import react_loop
from kronos.graph import KronosAgent
from kronos.session import SessionStore


def _make_model(responses: list[AIMessage]):
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=list(responses))
    bound = AsyncMock()
    bound.ainvoke = AsyncMock(side_effect=list(responses))
    model.bind_tools = MagicMock(return_value=bound)
    return model


def _ai_with_tool_call(tool_name: str, tool_call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {}, "id": tool_call_id}],
    )


def _minimal_agent(session_store: SessionStore) -> KronosAgent:
    agent = object.__new__(KronosAgent)
    agent._tools = []
    agent._enable_memory = False
    agent._memory_enabled = False
    agent._supervisor = None
    agent._system_prompt = "You are a test agent."
    agent._session_store = session_store
    agent._skill_store = None
    agent._external_tool_event_callback = None
    agent._durable_recovery_checked = False
    return agent


def _active_turn_statuses(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute("SELECT status FROM active_turns ORDER BY started_at")
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_session_store_recovers_abandoned_turn_into_history(tmp_path: Path) -> None:
    db_path = tmp_path / "session.db"
    store = SessionStore(str(db_path))
    metrics: list[tuple[str, int]] = []
    store._record_durable_metric = lambda metric, delta: metrics.append((metric, delta))
    await store.save("thread", [HumanMessage(content="before")])
    turn_id = await store.begin_turn("thread", "do the thing")
    tool_request = AIMessage(
        content="",
        tool_calls=[{"name": "write_side_effect", "args": {}, "id": "call_side_effect"}],
    )
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id="thread",
        messages=[
            tool_request,
            ToolMessage(content="side-effect complete", tool_call_id="call_side_effect"),
        ],
    )

    recovered = await store.recover_abandoned_turns()

    assert recovered == 1
    saved = await store.load("thread")
    assert [message.content for message in saved] == [
        "before",
        "do the thing",
        "",
        "side-effect complete",
        (
            "⚠️ Предыдущий ход был прерван до завершения. "
            "Я восстановил уже записанные шаги из журнала, "
            "но не продолжаю его автоматически."
        ),
    ]
    assert _active_turn_statuses(db_path) == ["recovered"]
    assert metrics == [("durable_turns_recovered", 1)]


@pytest.mark.asyncio
async def test_react_loop_uses_memoized_tool_result_without_execution() -> None:
    calls = 0

    async def counted_tool() -> str:
        nonlocal calls
        calls += 1
        return "fresh"

    tool = StructuredTool.from_function(
        coroutine=counted_tool,
        name="counted",
        description="count calls",
    )
    model = _make_model([
        _ai_with_tool_call("counted", tool_call_id="call_cached"),
        AIMessage(content="done"),
    ])
    saved_results: list[tuple[str, str]] = []

    result = await react_loop(
        model,
        [HumanMessage(content="run tool")],
        tools=[tool],
        get_cached_tool_result=lambda tool_call_id: "cached" if tool_call_id == "call_cached" else None,
        save_tool_result=lambda tool_call_id, content: saved_results.append((tool_call_id, content)),
    )

    assert result.content == "done"
    assert calls == 0
    assert saved_results == []
    tool_messages = [message for message in result.messages if isinstance(message, ToolMessage)]
    assert [message.content for message in tool_messages] == ["cached"]


@pytest.mark.asyncio
async def test_graph_recovery_runs_before_loading_next_turn(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "session.db"))
    store._record_durable_metric = lambda metric, delta: None
    turn_id = await store.begin_turn("thread", "interrupted")
    await store.append_turn_messages(
        turn_id=turn_id,
        thread_id="thread",
        messages=[AIMessage(content="partial answer")],
    )
    agent = _minimal_agent(store)

    with patch("kronos.graph.react_loop", new=AsyncMock(return_value=type("Result", (), {"content": "next ok"})())):
        reply = await agent.ainvoke(
            message="next",
            thread_id="thread",
            user_id="u",
            session_id="s",
        )

    assert reply == "next ok"
    saved = await store.load("thread")
    assert [message.content for message in saved] == [
        "interrupted",
        "partial answer",
        (
            "⚠️ Предыдущий ход был прерван до завершения. "
            "Я восстановил уже записанные шаги из журнала, "
            "но не продолжаю его автоматически."
        ),
        "next",
        "next ok",
    ]


@pytest.mark.asyncio
async def test_ephemeral_peer_reaction_does_not_open_durable_turn(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "session.db"))
    agent = _minimal_agent(store)
    store.begin_turn = AsyncMock(side_effect=AssertionError("ephemeral turn must not journal"))

    with patch("kronos.graph.react_loop", new=AsyncMock(return_value=type("Result", (), {"content": "delta"})())):
        reply = await agent.ainvoke(
            message="peer context",
            thread_id="thread",
            user_id="u",
            session_id="s",
            source_kind="peer_reaction",
            persist_user_turn=False,
        )

    assert reply == "delta"
    assert await store.load("thread") == []
