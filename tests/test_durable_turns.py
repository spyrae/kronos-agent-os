import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
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
    agent._last_pending_approval_id = None
    return agent


def _active_turn_statuses(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT status FROM active_turns ORDER BY started_at")]
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
    model = _make_model(
        [
            _ai_with_tool_call("counted", tool_call_id="call_cached"),
            AIMessage(content="done"),
        ]
    )
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

    with (
        patch("kronos.graph.get_model", return_value=MagicMock()),
        patch("kronos.graph.react_loop", new=AsyncMock(return_value=type("Result", (), {"content": "next ok"})())),
    ):
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

    with (
        patch("kronos.graph.get_model", return_value=MagicMock()),
        patch("kronos.graph.react_loop", new=AsyncMock(return_value=type("Result", (), {"content": "delta"})())),
    ):
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


@pytest.mark.asyncio
async def test_pending_approval_claims_exactly_once(tmp_path: Path) -> None:
    db_path = tmp_path / "session.db"
    store = SessionStore(str(db_path))
    turn_id = await store.begin_turn("thread", "mutate")

    approval_id = await store.create_pending_approval(
        turn_id=turn_id,
        thread_id="thread",
        tool_call_id="call_1",
        tool_name="mcp_add_server",
        args={"name": "demo"},
    )

    pending = await store.get_pending_approval(approval_id)
    assert pending is not None
    assert pending["status"] == "pending"
    assert pending["args"] == {"name": "demo"}
    assert _active_turn_statuses(db_path) == ["waiting_approval"]

    claimed = await store.claim_pending_approval(
        approval_id=approval_id,
        decision="approved",
        decided_by="42",
    )
    second_claim = await store.claim_pending_approval(
        approval_id=approval_id,
        decision="approved",
        decided_by="42",
    )

    assert claimed is not None
    assert claimed["tool_name"] == "mcp_add_server"
    assert claimed["status"] == "approved"
    assert second_claim is None
    assert _active_turn_statuses(db_path) == ["running"]


@pytest.mark.asyncio
async def test_agent_tool_executes_without_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    db_path = tmp_path / "session.db"
    store = SessionStore(str(db_path))
    calls = 0

    async def risky_tool() -> str:
        nonlocal calls
        calls += 1
        return "executed once"

    tool = StructuredTool.from_function(
        coroutine=risky_tool,
        name="mcp_add_server",
        description="mutates MCP servers",
    )
    model = _make_model(
        [
            _ai_with_tool_call("mcp_add_server", tool_call_id="call_approve"),
            AIMessage(content="done after approve"),
        ]
    )
    agent = _minimal_agent(store)
    agent._tools = [tool]

    with patch("kronos.graph.get_model", return_value=model):
        reply = await agent.ainvoke(
            message="add mcp server",
            thread_id="thread",
            user_id="u",
            session_id="s",
        )

    assert calls == 1
    assert reply == "done after approve"
    assert "Approval ID" not in reply
    assert agent.last_pending_approval_id is None
    saved = await store.load("thread")
    assert [message.content for message in saved] == [
        "add mcp server",
        "done after approve",
    ]
    assert _active_turn_statuses(db_path) == ["done"]


@pytest.mark.asyncio
async def test_agent_tool_series_executes_without_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = SessionStore(str(tmp_path / "session.db"))
    calls = 0

    async def replace_tranche() -> str:
        nonlocal calls
        calls += 1
        return f"tranche updated {calls}"

    tool = StructuredTool.from_function(
        coroutine=replace_tranche,
        name="replace_tranche",
        description="updates an existing tranche",
    )
    model = _make_model(
        [
            _ai_with_tool_call("replace_tranche", tool_call_id="call_15"),
            _ai_with_tool_call("replace_tranche", tool_call_id="call_16"),
            AIMessage(content="готово"),
        ]
    )
    agent = _minimal_agent(store)
    agent._tools = [tool]

    with patch("kronos.graph.get_model", return_value=model):
        reply = await agent.ainvoke(
            message="обнови транши 15 и 16",
            thread_id="thread",
            user_id="u",
            session_id="s",
        )

    assert calls == 2
    assert reply == "готово"
    assert "Approval ID" not in reply
    assert agent.last_pending_approval_id is None
    saved = await store.load("thread")
    assert [message.content for message in saved] == [
        "обнови транши 15 и 16",
        "готово",
    ]
    assert _active_turn_statuses(tmp_path / "session.db") == ["done"]


@pytest.mark.asyncio
async def test_agent_different_tools_execute_without_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = SessionStore(str(tmp_path / "session.db"))
    replace_calls = 0
    remove_calls = 0

    async def replace_tranche() -> str:
        nonlocal replace_calls
        replace_calls += 1
        return "tranche updated"

    async def remove_server() -> str:
        nonlocal remove_calls
        remove_calls += 1
        return "server removed"

    replace_tool = StructuredTool.from_function(
        coroutine=replace_tranche,
        name="replace_tranche",
        description="updates an existing tranche",
    )
    remove_tool = StructuredTool.from_function(
        coroutine=remove_server,
        name="mcp_remove_server",
        description="removes a server",
    )
    model = _make_model(
        [
            _ai_with_tool_call("replace_tranche", tool_call_id="call_tranche"),
            _ai_with_tool_call("mcp_remove_server", tool_call_id="call_remove"),
            AIMessage(content="all done"),
        ]
    )
    agent = _minimal_agent(store)
    agent._tools = [replace_tool, remove_tool]

    with patch("kronos.graph.get_model", return_value=model):
        reply = await agent.ainvoke(
            message="обнови транш и удали сервер",
            thread_id="thread",
            user_id="u",
            session_id="s",
        )

    assert replace_calls == 1
    assert remove_calls == 1
    assert reply == "all done"
    assert "Approval ID" not in reply
    assert agent.last_pending_approval_id is None


@pytest.mark.asyncio
async def test_agent_remove_tool_executes_without_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = SessionStore(str(tmp_path / "session.db"))
    calls = 0

    async def risky_tool() -> str:
        nonlocal calls
        calls += 1
        return "server removed"

    tool = StructuredTool.from_function(
        coroutine=risky_tool,
        name="mcp_remove_server",
        description="mutates MCP servers",
    )
    model = _make_model(
        [
            _ai_with_tool_call("mcp_remove_server", tool_call_id="call_reject"),
            AIMessage(content="removed"),
        ]
    )
    agent = _minimal_agent(store)
    agent._tools = [tool]

    with patch("kronos.graph.get_model", return_value=model):
        reply = await agent.ainvoke(
            message="remove server",
            thread_id="thread",
            user_id="u",
            session_id="s",
        )

    assert "Approval ID" not in reply
    assert agent.last_pending_approval_id is None
    assert calls == 1
    assert reply == "removed"
    saved = await store.load("thread")
    assert [message.content for message in saved] == [
        "remove server",
        "removed",
    ]


async def test_ainvoke_forwards_per_call_tool_events(tmp_path: Path, monkeypatch) -> None:
    # roadmap 4.1: a per-call on_tool_event (bridge live progress) must receive
    # tool events, alongside the agent-level audit callback.
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = SessionStore(str(tmp_path / "session.db"))
    events: list[tuple[str, str]] = []

    async def do_thing() -> str:
        return "ok"

    tool = StructuredTool.from_function(
        coroutine=do_thing, name="do_thing", description="a tool"
    )
    model = _make_model(
        [_ai_with_tool_call("do_thing", tool_call_id="c1"), AIMessage(content="done")]
    )
    agent = _minimal_agent(store)
    agent._tools = [tool]

    with patch("kronos.graph.get_model", return_value=model):
        reply = await agent.ainvoke(
            message="go",
            thread_id="thread",
            user_id="u",
            session_id="s",
            on_tool_event=lambda ev, payload: events.append((ev, payload.get("name"))),
        )

    assert reply == "done"
    assert ("tool_call", "do_thing") in events
    assert ("tool_result", "do_thing") in events


async def test_force_tier_overrides_classification(tmp_path: Path, monkeypatch) -> None:
    # roadmap 6.2: cost-guardian degradation forces the lite tier regardless of
    # what classify_tier would otherwise pick for the message.
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = SessionStore(str(tmp_path / "session.db"))
    agent = _minimal_agent(store)
    captured: dict[str, str] = {}
    model = _make_model([AIMessage(content="ok")])

    def fake_get_model(tier):
        captured["tier"] = tier
        return model

    with patch("kronos.graph.get_model", side_effect=fake_get_model):
        await agent.ainvoke(
            message="сделай глубокий аналитический разбор рынка",
            thread_id="thread",
            user_id="u",
            session_id="s",
            force_tier="lite",
        )

    assert captured["tier"] == "lite"
