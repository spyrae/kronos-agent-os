"""Sub-agent approval propagation (nested durable approval through delegation).

Before this, an approval-worthy tool (restart_/delete_/send_/add_expense/…)
called *inside* a delegated sub-agent executed with no approval — the gate was
top-level only. Now the sub-agent shares the parent's approval channel, the
pause bubbles up as a top-level pause, and the resume re-runs the delegation
with the approved call exempted.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from kronos.agents.supervisor import _make_delegation_tool
from kronos.engine import create_agent, current_delegation, react_loop


def _make_model(responses: list[AIMessage]):
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=list(responses))
    bound = AsyncMock()
    bound.ainvoke = AsyncMock(side_effect=list(responses))
    model.bind_tools = MagicMock(return_value=bound)
    return model


def _ai_tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _make_tool(name: str, result: str, calls: list) -> StructuredTool:
    async def _fn(**kwargs) -> str:
        calls.append((name, kwargs))
        return result

    return StructuredTool.from_function(coroutine=_fn, name=name, description=f"tool {name}")


def _server_ops_delegation(sub_responses: list[AIMessage], executed: list):
    """A delegate_to_server_ops tool wrapping a sub-agent whose only tool is the
    approval-worthy restart_service."""
    restart = _make_tool("restart_service", result="restarted", calls=executed)
    sub_model = _make_model(sub_responses)
    sub_agent = create_agent(sub_model, [restart], "sub prompt", "server_ops_agent")
    return _make_delegation_tool("server_ops", "delegate to server ops", sub_agent)


@pytest.fixture(autouse=True)
def _approvals_on(monkeypatch):
    from kronos.config import settings
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)


# --------------------------------------------------------------------------
# Bubble-up: a sub-agent's approval-worthy tool pauses the whole turn
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subagent_approval_bubbles_up_and_tags_delegation():
    executed: list = []
    delegation_tool = _server_ops_delegation(
        [_ai_tool_call("restart_service", {"host": "web1"}, "call_restart")],
        executed,
    )

    recorded: dict = {}

    def request_approval(tool, tool_call):
        recorded["tool"] = tool.name
        recorded["delegation"] = current_delegation()
        return "apr_nested_1"

    top_model = _make_model([_ai_tool_call("delegate_to_server_ops", {"request": "restart web1"}, "call_deleg")])

    result = await react_loop(
        model=top_model,
        messages=[HumanMessage(content="restart web1")],
        tools=[delegation_tool],
        request_tool_approval=request_approval,
    )

    # The turn paused at the TOP level, naming the sub-agent's tool.
    assert result.waiting_approval is True
    assert result.approval_id == "apr_nested_1"
    assert result.approval_tool_name == "restart_service"
    # The approval-worthy tool did NOT execute silently.
    assert executed == []
    # The pending approval was tagged with the parent delegate_to_X call.
    assert recorded["tool"] == "restart_service"
    assert recorded["delegation"]["tool_name"] == "delegate_to_server_ops"
    assert recorded["delegation"]["tool_call_id"] == "call_deleg"
    assert recorded["delegation"]["request"] == "restart web1"


@pytest.mark.asyncio
async def test_subagent_safe_tool_runs_without_pause():
    """A non-approval-worthy sub-agent tool executes normally (no false pause)."""
    executed: list = []
    search = _make_tool("search_logs", result="42 lines", calls=executed)
    sub_model = _make_model([
        _ai_tool_call("search_logs", {"q": "err"}, "call_s"),
        AIMessage(content="found 42 error lines"),
    ])
    sub_agent = create_agent(sub_model, [search], "sub", "server_ops_agent")
    delegation_tool = _make_delegation_tool("server_ops", "delegate", sub_agent)

    top_model = _make_model([
        _ai_tool_call("delegate_to_server_ops", {"request": "search errors"}, "call_deleg"),
        AIMessage(content="done"),
    ])

    result = await react_loop(
        model=top_model,
        messages=[HumanMessage(content="search errors")],
        tools=[delegation_tool],
        request_tool_approval=lambda tool, tc: "should_not_be_used",
    )

    assert result.waiting_approval is False
    assert executed and executed[0][0] == "search_logs"  # ran without approval


# --------------------------------------------------------------------------
# Resume: re-delegate with the approved sub-call exempted
# --------------------------------------------------------------------------

def _fake_agent(delegation_tool: StructuredTool):
    """Minimal object exposing the two attrs _resume_delegated_approval needs."""
    from kronos.graph import KronosAgent

    agent = object.__new__(KronosAgent)
    agent._approval_tool_map = lambda: {delegation_tool.name: delegation_tool}
    agent._session_store = MagicMock()
    agent._session_store.save_tool_result = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_resume_reruns_delegation_with_exemption():
    executed: list = []
    delegation_tool = _server_ops_delegation(
        [
            _ai_tool_call("restart_service", {"host": "web1"}, "call_restart"),
            AIMessage(content="restarted web1 ✓"),
        ],
        executed,
    )
    agent = _fake_agent(delegation_tool)

    async def exemption(tool, args):
        # Exempt exactly the approved restart_service(host=web1); anything else
        # would still require approval.
        return not (tool.name == "restart_service" and args == {"host": "web1"})

    resumed = await agent.__class__._resume_delegated_approval(
        agent,
        approved=True,
        turn_id="turn_1",
        delegation={
            "tool_name": "delegate_to_server_ops",
            "tool_call_id": "call_deleg",
            "request": "restart web1",
        },
        request_tool_approval=lambda tool, tc: "unused",
        needs_tool_approval=exemption,
    )

    # Re-delegation ran, the approved tool executed, its result fills the
    # delegate_to_X call's ToolMessage.
    assert executed and executed[0][0] == "restart_service"
    tool_message = resumed["tool_message"]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_deleg"
    assert "restarted web1" in tool_message.content
    agent._session_store.save_tool_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_rejection_short_circuits_delegation():
    executed: list = []
    delegation_tool = _server_ops_delegation(
        [_ai_tool_call("restart_service", {"host": "web1"}, "call_restart")],
        executed,
    )
    agent = _fake_agent(delegation_tool)

    resumed = await agent.__class__._resume_delegated_approval(
        agent,
        approved=False,
        turn_id="turn_1",
        delegation={"tool_name": "delegate_to_server_ops", "tool_call_id": "call_deleg", "request": "restart web1"},
        request_tool_approval=lambda tool, tc: "unused",
        needs_tool_approval=None,
    )

    assert executed == []  # nothing ran
    assert resumed["tool_message"].content == "[REJECTED by user]"
    assert resumed["tool_message"].tool_call_id == "call_deleg"


# --------------------------------------------------------------------------
# Session store round-trips the delegation context
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pending_approval_round_trips_delegation(tmp_path: Path):
    from kronos.session import SessionStore

    store = SessionStore(str(tmp_path / "session.db"))
    turn_id = await store.begin_turn("thread-1", "restart web1")
    delegation = {"tool_name": "delegate_to_server_ops", "tool_call_id": "call_deleg", "request": "restart web1"}

    approval_id = await store.create_pending_approval(
        turn_id=turn_id,
        thread_id="thread-1",
        tool_call_id="call_restart",
        tool_name="restart_service",
        args={"host": "web1"},
        delegation=delegation,
    )

    fetched = await store.get_pending_approval(approval_id)
    assert fetched["delegation"] == delegation

    claimed = await store.claim_pending_approval(approval_id=approval_id, decision="approved")
    assert claimed["delegation"] == delegation


@pytest.mark.asyncio
async def test_pending_approvals_migration_from_old_schema(tmp_path: Path):
    """A DB created before delegation_json existed migrates cleanly, and a
    second init over the migrated DB is a no-op (race-safe: the ALTER swallows
    a concurrent duplicate-column add)."""
    import aiosqlite

    from kronos.session import SessionStore

    db_path = str(tmp_path / "session.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE pending_approvals ("
            "approval_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
            "tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, args_json TEXT NOT NULL DEFAULT '{}', "
            "status TEXT NOT NULL DEFAULT 'pending', requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "decided_at TIMESTAMP, decided_by TEXT, decision TEXT)"
        )
        await db.commit()

    store = SessionStore(db_path)
    turn_id = await store.begin_turn("t", "hi")  # first use migrates (adds the column)
    approval_id = await store.create_pending_approval(
        turn_id=turn_id, thread_id="t", tool_call_id="c",
        tool_name="restart_service", args={}, delegation={"tool_name": "delegate_to_x"},
    )
    assert (await store.get_pending_approval(approval_id))["delegation"] == {"tool_name": "delegate_to_x"}

    # A second store re-running _ensure_table over the migrated DB must not raise.
    store2 = SessionStore(db_path)
    await store2.begin_turn("t2", "hi")
