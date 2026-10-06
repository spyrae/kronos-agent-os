"""The data owner of a turn comes from the transport, never from the approver or the model."""

import sqlite3
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.memory.scope import current_data_owner, data_owner_scope
from kronos.migrations.v009_turn_owner import migrate
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
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    yield obj
    db_module._instances.clear()
    swarm_module._singleton = None


def _model(monkeypatch, responses):
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=responses)
    model.bind_tools = lambda tools: model
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
    return model


def _call(name, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])


def _probes(agent):
    """A sync read-only probe and an async approval-gated one, both recording the bound owner."""
    seen: list[tuple[str, str]] = []

    def whoami() -> str:
        seen.append(("whoami", current_data_owner()))
        return "ok"

    async def restart_service() -> str:
        seen.append(("restart_service", current_data_owner()))
        return "restarted"

    agent._tools = [
        StructuredTool.from_function(func=whoami, name="whoami", description="read-only probe"),
        StructuredTool.from_function(coroutine=restart_service, name="restart_service", description="gated probe"),
    ]
    return seen


def test_scope_is_empty_by_default_and_restored_after_exit():
    assert current_data_owner() == ""
    with data_owner_scope("alice"):
        assert current_data_owner() == "alice"
        with data_owner_scope(""):
            assert current_data_owner() == ""
        assert current_data_owner() == "alice"
    assert current_data_owner() == ""


async def test_tool_sees_the_transport_user_including_sync_tools(agent, monkeypatch):
    seen = _probes(agent)
    _model(monkeypatch, [_call("whoami"), AIMessage(content="done")])
    outcome = await agent.ainvoke_outcome("hi", "thread", user_id="alice")
    assert outcome.status == "completed"
    assert seen == [("whoami", "alice")]
    assert await agent._session_store.turn_owner(outcome.turn_id) == "alice"
    assert current_data_owner() == ""


async def test_approver_does_not_become_the_data_owner(agent, monkeypatch):
    seen = _probes(agent)
    _model(monkeypatch, [_call("restart_service"), _call("whoami", "c2"), AIMessage(content="done")])
    paused = await agent.ainvoke_outcome("do it", "thread", user_id="alice")
    assert paused.status == "waiting_approval" and seen == []

    agent._session_store = SessionStore(agent._session_store.db_path)
    await agent.resolve_tool_approval(paused.approval_id, approved=True, decided_by="owner")

    assert seen == [("restart_service", "alice"), ("whoami", "alice")]
    assert (await agent.get_turn_outcome(paused.turn_id)).status == "completed"


async def test_resume_restores_the_owner_recorded_at_begin(agent, monkeypatch):
    seen = _probes(agent)
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = agent._session_store
    turn_id = await store.begin_turn("thread", "hi", user_id="alice")
    await store.append_turn_messages(turn_id=turn_id, thread_id="thread", messages=[HumanMessage(content="hi")])
    _model(monkeypatch, [_call("whoami"), AIMessage(content="done")])

    assert await agent.resume_interrupted_turn(turn_id, notify=False) == "done"
    assert seen == [("whoami", "alice")]


async def test_resume_of_an_ownerless_turn_binds_nobody(agent, monkeypatch):
    seen = _probes(agent)
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    store = agent._session_store
    turn_id = await store.begin_turn("thread", "hi")
    await store.append_turn_messages(turn_id=turn_id, thread_id="thread", messages=[HumanMessage(content="hi")])
    _model(monkeypatch, [_call("whoami"), AIMessage(content="done")])

    with data_owner_scope("someone-else"):
        await agent.resume_interrupted_turn(turn_id, notify=False)
    assert seen == [("whoami", "")]


async def test_migration_leaves_legacy_turns_ownerless_and_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE active_turns (turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, status TEXT NOT NULL,"
            " input_message TEXT NOT NULL, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " completed_at TIMESTAMP, error TEXT)"
        )
        db.execute("INSERT INTO active_turns (turn_id, thread_id, status, input_message) VALUES ('old', 't', 'done', 'x')")

    async with aiosqlite.connect(path) as db:
        await migrate(db)
        await migrate(db)

    store = SessionStore(path)
    assert await store.turn_owner("old") == ""
    assert await store.turn_owner("missing") == ""
    fresh = await store.begin_turn("t", "new", user_id=" bob ")
    assert await store.turn_owner(fresh) == "bob"
