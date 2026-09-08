"""One live executor across entry points, processes and coroutine cancellation."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from kronos.config import settings
from kronos.effect_state import DurableStateError
from kronos.engine import AgentResult
from kronos.graph import KronosAgent
from kronos.session import SessionStore
from kronos.turn_ownership import TurnBusyError, own_conversation


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    import kronos.db as db_module

    db_module._instances.clear()

    def make():
        obj = object.__new__(KronosAgent)
        obj._session_store = SessionStore(settings.db_path)
        obj._memory_enabled = False
        obj._supervisor = None
        obj._tools = []
        obj._skill_store = None
        obj._system_prompt = "system"
        obj._external_tool_event_callback = None
        obj._durable_recovery_checked = False
        obj._last_pending_approval_id = None
        return obj

    yield make
    db_module._instances.clear()


def _gated_loop(agent):
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(*, messages, **kwargs):
        entered.set()
        await release.wait()
        return AgentResult(content="done", messages=messages + [AIMessage(content="done")])

    agent._run_model_loop = AsyncMock(side_effect=run)
    return entered, release


async def test_live_invocation_cannot_be_resumed_or_reported(agents):
    live, other = agents(), agents()
    entered, release = _gated_loop(live)
    other._run_model_loop = AsyncMock(return_value=AgentResult(content="other", messages=[]))
    task = asyncio.create_task(live.ainvoke_outcome("first", "chat"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        turn = (await other._session_store.resumable_turns())[0]
        assert await other._session_store.recover_abandoned_turns() == 0
        assert await other.resume_abandoned_turns() == 0
        with pytest.raises(TurnBusyError):
            await other.resume_interrupted_turn(turn)
        # First-call recovery of another agent also leaves the live turn alone.
        assert (await other.ainvoke_outcome("unrelated", "other")).status == "completed"
        assert (await other.get_turn_outcome(turn["turn_id"])).status == "running"
        assert (await other._session_store.get_turn_detail(turn["turn_id"]))["attempts"] == 0
    finally:
        release.set()
        result = await asyncio.wait_for(task, 2)
    assert result.status == "completed"


async def test_resume_race_and_new_user_turn_share_ownership(agents):
    first, second = agents(), agents()
    store = first._session_store
    turn_id = await store.begin_turn("chat", "original")
    entered, release = _gated_loop(first)
    second._run_model_loop = AsyncMock(return_value=AgentResult(content="next", messages=[]))
    resumed = asyncio.create_task(first.resume_interrupted_turn(turn_id))
    next_turn = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(TurnBusyError):
            await second.resume_interrupted_turn(turn_id)
        next_turn = asyncio.create_task(second.ainvoke_outcome("new question", "chat"))
        await asyncio.sleep(0.1)
        second._run_model_loop.assert_not_awaited()
        assert (await store.get_turn_detail(turn_id))["attempts"] == 1
    finally:
        release.set()
        assert await asyncio.wait_for(resumed, 2) == "done"
        if next_turn:
            await asyncio.wait_for(next_turn, 2)
    assert await second.resume_interrupted_turn(turn_id) is None
    assert [m.content for m in await store.load("chat")] == ["original", "done", "new question", "next"]


async def test_cancelled_resume_can_be_claimed_again_without_status_rewrite(agents):
    first, second = agents(), agents()
    turn_id = await first._session_store.begin_turn("chat", "original")
    entered, _ = _gated_loop(first)
    task = asyncio.create_task(first.resume_interrupted_turn(turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await first._session_store.get_turn_detail(turn_id))["status"] == "resuming"
    second._run_model_loop = AsyncMock(return_value=AgentResult(content="recovered", messages=[]))
    assert await second.resume_interrupted_turn(turn_id) == "recovered"
    assert (await first._session_store.get_turn_detail(turn_id))["attempts"] == 2


async def test_resume_uses_database_identity_not_caller_snapshot(agents):
    agent = agents()
    turn_id = await agent._session_store.begin_turn("real-thread", "original")
    agent._run_model_loop = AsyncMock(return_value=AgentResult(content="done", messages=[]))
    assert await agent.resume_interrupted_turn(
        {"turn_id": turn_id, "thread_id": "wrong-thread", "input_message": "injected", "attempts": -100}
    ) == "done"
    kwargs = agent._run_model_loop.await_args.kwargs
    assert kwargs["source_message"] == "original"
    assert [m.content for m in kwargs["messages"]] == ["original"]
    assert (await agent._session_store.get_turn_detail(turn_id))["attempts"] == 1
    assert await agent._session_store.load("wrong-thread") == []


async def test_cancelled_approval_releases_lock_but_unresolved_effect_still_blocks_resume(agents):
    first, second = agents(), agents()
    store = first._session_store
    turn_id = await store.begin_turn("chat", "write")
    entered = asyncio.Event()
    calls = 0

    async def write():
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()
        return "done"

    first._tools = [StructuredTool.from_function(coroutine=write, name="send_message", description="write")]
    approval = await store.create_pending_approval(
        turn_id=turn_id, thread_id="chat", tool_call_id="c1", tool_name="send_message", args={}
    )
    task = asyncio.create_task(first.resolve_tool_approval(approval, True))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(TurnBusyError):
            await second.resume_interrupted_turn(turn_id)
        assert await second._session_store.recover_abandoned_turns() == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    second._run_model_loop = AsyncMock()
    assert await second.resume_interrupted_turn(turn_id) is None
    second._run_model_loop.assert_not_awaited()
    assert calls == 1
    assert (await store.list_external_effects(turn_id))[0]["status"] == "pending"


async def test_pending_approval_cannot_be_bypassed_by_inconsistent_running_flag(agents):
    agent = agents()
    store = agent._session_store
    turn_id = await store.begin_turn("chat", "write")
    await store.create_pending_approval(
        turn_id=turn_id, thread_id="chat", tool_call_id="c1", tool_name="send_message", args={}
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET status = 'running' WHERE turn_id = ?", (turn_id,))
    agent._run_model_loop = AsyncMock()
    assert await agent.resume_interrupted_turn(turn_id) is None
    assert await store.recover_abandoned_turns() == 0
    assert (await store.get_turn_detail(turn_id))["attempts"] == 0
    agent._run_model_loop.assert_not_awaited()


async def test_claim_requires_correct_live_task_local_ownership(agents):
    store = agents()._session_store
    turn_id = await store.begin_turn("chat", "work")
    async with own_conversation(store.db_path, "wrong") as wrong:
        with pytest.raises(DurableStateError):
            await store.claim_turn_for_resume(turn_id, ownership=wrong)
    async with own_conversation(store.db_path, "chat") as ownership:
        with pytest.raises(DurableStateError):
            await asyncio.create_task(store.claim_turn_for_resume(turn_id, ownership=ownership))
    with pytest.raises(DurableStateError):
        await store.claim_turn_for_resume(turn_id, ownership=ownership)
    assert (await store.get_turn_detail(turn_id))["attempts"] == 0


async def test_lock_identity_resolves_aliases_and_keeps_inode(tmp_path):
    path = tmp_path / "session.db"
    path.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    async with own_conversation(str(path), "private/chat/name"):
        with pytest.raises(TurnBusyError):
            async with own_conversation(str(alias), "private/chat/name", wait=False):
                pytest.fail("symlink bypass")
        lock = next((tmp_path / ".session.db.turn-locks").iterdir())
        inode = lock.stat().st_ino
        assert lock.stat().st_mode & 0o777 == 0o600
        assert "private" not in lock.name
    async with own_conversation(str(path), "private/chat/name"):
        assert lock.stat().st_ino == inode


async def test_cancelling_waiter_does_not_release_the_owner(tmp_path):
    path = str(tmp_path / "session.db")

    async def waiter():
        async with own_conversation(path, "thread"):
            pytest.fail("waiter must not acquire the lock")

    async with own_conversation(path, "thread"):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(TurnBusyError):
            async with own_conversation(path, "thread", wait=False):
                pytest.fail("cancelled waiter released another executor")
        async with own_conversation(path, "different", wait=False):
            pass
    async with own_conversation(path, "thread", wait=False):
        pass


async def test_lock_error_fails_before_model_or_turn_creation(agents, monkeypatch):
    agent = agents()
    agent._durable_recovery_checked = True
    agent._run_model_loop = AsyncMock()

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("kronos.turn_ownership.os.open", denied)
    with pytest.raises(DurableStateError):
        await agent.ainvoke_outcome("write", "chat")
    agent._run_model_loop.assert_not_awaited()
    assert await agent._session_store.resumable_turns() == []


async def test_failed_resume_response_is_not_counted_as_completed(agents):
    agent = agents()
    await agent._session_store.begin_turn("chat", "work")
    agent._run_model_loop = AsyncMock(
        return_value=AgentResult(content="failed", messages=[], failure_reason="model_error")
    )
    assert await agent.resume_abandoned_turns() == 0


async def test_concurrent_report_recovers_history_once(agents):
    first, second = agents(), agents()
    turn_id = await first._session_store.begin_turn("chat", "interrupted")
    counts = await asyncio.gather(
        first._session_store.recover_abandoned_turns(), second._session_store.recover_abandoned_turns()
    )
    assert sum(counts) == 1
    assert len(await first._session_store.load("chat")) == 2
    assert (await first._session_store.get_turn_detail(turn_id))["status"] == "recovered"


async def test_dashboard_resumes_with_live_tools_and_refuses_racing_request(agents, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from dashboard.auth import verify_token
    from dashboard.server import create_app

    agent = agents()
    store = agent._session_store
    turn_id = await store.begin_turn("chat", "write")
    await store.append_turn_messages(
        turn_id=turn_id, thread_id="chat",
        messages=[AIMessage(content="", tool_calls=[{"name": "send_message", "args": {}, "id": "c1"}])],
    )
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def send():
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return "sent"

    agent._tools = [StructuredTool.from_function(coroutine=send, name="send_message", description="live tool")]
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[{"name": "send_message", "args": {}, "id": "c1"}]),
        AIMessage(content="done"),
    ])
    model.bind_tools = lambda tools: model
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)

    def no_new_agent(*args, **kwargs):
        pytest.fail("dashboard constructed another agent instead of using its live registry")

    monkeypatch.setattr(KronosAgent, "__init__", no_new_agent)
    app = create_app(agent=agent)
    app.dependency_overrides[verify_token] = lambda: True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post(f"/api/turns/{turn_id}/resume"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await client.post(f"/api/turns/{turn_id}/resume")
            assert second.status_code == 409
            assert "live executor" in second.json()["detail"]
            assert (await store.get_turn_detail(turn_id))["attempts"] == 1
        finally:
            release.set()
            response = await asyncio.wait_for(first, 2)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert count == 1
