"""F13: a cleared conversation stays cleared, in every store and under a race."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.session import SessionStore


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    obj = object.__new__(KronosAgent)
    obj._session_store = SessionStore(str(tmp_path / "session.db"), agent_name="kronos")
    obj._memory_enabled = False
    obj._supervisor = None
    obj._tools = []
    obj._skill_store = None
    obj._system_prompt = "system"
    obj._external_tool_event_callback = None
    obj._durable_recovery_checked = True
    obj._last_pending_approval_id = None
    yield obj
    db_module._instances.clear()
    swarm_module._singleton = None


def _model(monkeypatch, responses):
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=responses)
    model.bind_tools = lambda tools: model
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
    return model


def _search(query, user_id="alice"):
    from kronos.swarm_store import get_swarm

    return [r["content"] for r in get_swarm().search_sessions(query=query, user_id=user_id)]


async def _history(agent, thread_id):
    return [m.content for m in await agent._session_store.load(thread_id)]


async def test_clear_removes_history_and_its_search_rows_only_for_that_thread(agent, monkeypatch):
    _model(monkeypatch, [AIMessage(content="noted the passport"), AIMessage(content="noted the invoice")])
    await agent.ainvoke("remember my passport number", "100", user_id="alice")
    await agent.ainvoke("remember the invoice total", "200", user_id="alice")
    assert _search("passport") and _search("invoice")

    reply = await agent.clear_context("100")

    assert "очищена" in reply
    assert await _history(agent, "100") == []
    assert _search("passport") == []
    assert _search("invoice") and await _history(agent, "200")


async def test_clear_waits_for_a_running_turn_so_it_cannot_write_history_back(agent, monkeypatch):
    _model(monkeypatch, [AIMessage(content="first answer")])
    await agent.ainvoke("old secret question", "100", user_id="alice")

    release = asyncio.Event()
    in_model = asyncio.Event()

    async def slow(*args, **kwargs):
        in_model.set()
        await release.wait()
        return AIMessage(content="late answer")

    model = _model(monkeypatch, [])
    model.ainvoke = AsyncMock(side_effect=slow)

    turn = asyncio.create_task(agent.ainvoke("new question", "100", user_id="alice"))
    await in_model.wait()
    clear = asyncio.create_task(agent.clear_context("100"))
    await asyncio.sleep(0.05)
    assert not clear.done(), "clear must not run under a live executor of the same thread"

    release.set()
    assert await turn == "late answer"
    await clear

    assert await _history(agent, "100") == []
    assert _search("secret") == [] and _search("late") == []


async def test_a_turn_parked_on_approval_does_not_restore_cleared_history(agent, monkeypatch):
    effect = AsyncMock(return_value="restarted")

    async def restart_service() -> str:
        return await effect()

    agent._tools = [StructuredTool.from_function(coroutine=restart_service, name="restart_service", description="x")]
    request = AIMessage(content="", tool_calls=[{"name": "restart_service", "args": {}, "id": "c1"}])
    _model(monkeypatch, [AIMessage(content="first answer"), request, AIMessage(content="service restarted")])

    await agent.ainvoke("old secret question", "100", user_id="alice")
    paused = await agent.ainvoke_outcome("restart it", "100", user_id="alice")
    assert paused.status == "waiting_approval"

    await agent.clear_context("100")
    assert await _history(agent, "100") == [] and _search("secret") == []

    await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner")

    history = await _history(agent, "100")
    assert "old secret question" not in history and "first answer" not in history
    assert history[0] == "restart it" and history[-1] == "service restarted"
    assert _search("secret") == []


async def test_clear_reports_failure_instead_of_claiming_success(agent, monkeypatch):
    import kronos.bridge as bridge
    from kronos.swarm_store import get_swarm

    _model(monkeypatch, [AIMessage(content="first answer")])
    await agent.ainvoke("old secret question", "100", user_id="alice")
    monkeypatch.setattr(bridge, "_agent", agent)

    def broken(**kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(get_swarm(), "clear_session_index", broken)
    reply = await bridge._clear_context(100)
    assert "Не удалось" in reply and "очищена" not in reply


async def test_store_without_an_agent_name_still_clears_history(tmp_path):
    store = SessionStore(str(tmp_path / "plain.db"))
    await store.save("t", [HumanMessage(content="hello there")])
    assert await store.clear("t") == 1
    assert await store.load("t") == []
