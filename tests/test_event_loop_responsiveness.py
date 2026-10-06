"""F15: slow memory and compaction work must not freeze the agent's event loop.

While one conversation waits on a vector search or on the model that compacts
its history, the bridge, the dashboard, the scheduler and every other
conversation share the same loop.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.memory.scope import current_data_owner
from kronos.session import SessionStore

BLOCK_SECONDS = 0.4
# A healthy loop ticks every few milliseconds; the old code stalled for BLOCK_SECONDS.
MAX_STALL_SECONDS = 0.15


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="answer"))
    model.bind_tools = lambda tools: model
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
    obj = object.__new__(KronosAgent)
    obj._session_store = SessionStore(str(tmp_path / "session.db"), agent_name="kronos")
    obj._memory_enabled = True
    obj._supervisor = None
    obj._tools = []
    obj._skill_store = None
    obj._system_prompt = "system"
    obj._external_tool_event_callback = None
    obj._durable_recovery_checked = True
    obj._last_pending_approval_id = None
    monkeypatch.setattr("kronos.graph.store_memories_background", lambda state: {})
    yield obj
    db_module._instances.clear()
    swarm_module._singleton = None


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """An isolated shared ledger for tests that do not need a whole agent."""
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    yield
    db_module._instances.clear()
    swarm_module._singleton = None


async def _longest_stall(work) -> tuple[float, object]:
    """Run ``work`` while a heartbeat measures the longest gap between loop ticks."""
    longest = 0.0
    stop = asyncio.Event()

    async def heartbeat():
        nonlocal longest
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            longest = max(longest, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    try:
        result = await work
    finally:
        stop.set()
        await beat
    return longest, result


async def test_slow_memory_retrieval_does_not_stall_the_loop(agent, monkeypatch):
    seen = {}

    def slow_retrieval(state):
        seen["thread"] = threading.current_thread() is threading.main_thread()
        seen["owner"] = current_data_owner()
        time.sleep(BLOCK_SECONDS)
        return {"messages": [SystemMessage(content="remembered fact")]}

    monkeypatch.setattr("kronos.graph.retrieve_memories", slow_retrieval)

    stall, reply = await _longest_stall(agent.ainvoke("hello", "thread", user_id="alice"))

    assert reply == "answer"
    assert stall < MAX_STALL_SECONDS, f"event loop stalled for {stall:.2f}s during memory retrieval"
    assert seen == {"thread": False, "owner": "alice"}, "the worker keeps the caller's scope"


async def test_slow_compaction_does_not_stall_the_loop(agent, monkeypatch):
    monkeypatch.setattr("kronos.graph.retrieve_memories", lambda state: {})

    class SlowEngine:
        def should_compact(self, state):
            return True

        def compact(self, state):
            time.sleep(BLOCK_SECONDS)
            return {"messages": [AIMessage(content="summary of the conversation")]}

    monkeypatch.setattr("kronos.graph.get_context_engine", lambda: SlowEngine())

    stall, reply = await _longest_stall(agent.ainvoke("hello", "thread", user_id="alice"))

    assert reply == "answer"
    assert stall < MAX_STALL_SECONDS, f"event loop stalled for {stall:.2f}s during compaction"
    assert [m.content for m in await agent._session_store.load("thread")] == ["summary of the conversation"]


async def test_another_conversation_is_answered_while_one_waits_on_memory(agent, monkeypatch):
    release = threading.Event()

    def retrieval(state):
        if state["user_id"] == "slow":
            assert release.wait(5)
        return {}

    monkeypatch.setattr("kronos.graph.retrieve_memories", retrieval)

    slow = asyncio.create_task(agent.ainvoke("hello", "slow-thread", user_id="slow"))
    await asyncio.sleep(0.05)
    assert await asyncio.wait_for(agent.ainvoke("hello", "fast-thread", user_id="fast"), 2) == "answer"
    assert not slow.done()
    release.set()
    assert await slow == "answer"


async def test_cancelling_the_turn_waits_for_the_worker_instead_of_abandoning_it(agent, monkeypatch):
    """The conversation stays owned until the sync call has really returned."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def retrieval(state):
        started.set()
        release.wait(5)
        finished.set()
        return {}

    monkeypatch.setattr("kronos.graph.retrieve_memories", retrieval)
    turn = asyncio.create_task(agent.ainvoke("hello", "thread", user_id="alice"))
    await asyncio.to_thread(started.wait, 5)

    turn.cancel()
    await asyncio.sleep(0.05)
    assert not turn.done(), "cancellation must not release the conversation under a running worker"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert finished.is_set()


async def test_budget_admission_waiting_on_the_ledger_does_not_stall_the_loop(ledger, monkeypatch):
    """Reserving takes the shared ledger's write lock; another process may hold it."""
    from kronos.audit import reset_tool_audit_context, set_tool_audit_context
    from kronos.security import cost_guardian, model_budget
    from kronos.security.cost_tracking import record_llm_cost
    from kronos.security.model_budget import BudgetedModel
    from kronos.swarm_store import get_swarm

    guardian = cost_guardian.CostGuardian(daily_limit=5.0, session_limit=1.0)
    monkeypatch.setattr(cost_guardian, "_guardian", guardian)
    monkeypatch.setattr(guardian, "should_degrade", lambda: False)
    real_reserve = guardian.reserve

    def slow_reserve(session_id=""):
        time.sleep(BLOCK_SECONDS)
        return real_reserve(session_id)

    monkeypatch.setattr(guardian, "reserve", slow_reserve)

    async def answer(*args, **kwargs):
        # The recorder runs in the caller's task and must see the worker's hold.
        assert model_budget.current_hold()
        record_llm_cost("model", 10, 5, 0.004)
        return AIMessage(content="ok")

    inner = AsyncMock()
    inner.ainvoke = AsyncMock(side_effect=answer)
    token = set_tool_audit_context(agent="kronos", session_id="chat", thread_id="chat")
    try:
        stall, _ = await _longest_stall(BudgetedModel(inner, label="standard", lite_factory=lambda: inner).ainvoke([]))
    finally:
        reset_tool_audit_context(token)

    assert stall < MAX_STALL_SECONDS, f"event loop stalled for {stall:.2f}s during budget admission"
    assert get_swarm().budget_holds() == {"held": 0.0, "unknown": 0.0}
    assert get_swarm().session_cost("chat") == pytest.approx(0.004)


async def test_a_call_cancelled_during_admission_does_not_leave_its_hold_behind(ledger, monkeypatch):
    from kronos.audit import reset_tool_audit_context, set_tool_audit_context
    from kronos.security import cost_guardian
    from kronos.security.model_budget import BudgetedModel
    from kronos.swarm_store import get_swarm

    guardian = cost_guardian.CostGuardian(daily_limit=5.0, session_limit=1.0)
    monkeypatch.setattr(cost_guardian, "_guardian", guardian)
    monkeypatch.setattr(guardian, "should_degrade", lambda: False)
    real_reserve, entered, release = guardian.reserve, threading.Event(), threading.Event()

    def gated_reserve(session_id=""):
        entered.set()
        release.wait(5)
        return real_reserve(session_id)

    monkeypatch.setattr(guardian, "reserve", gated_reserve)
    inner = AsyncMock()
    token = set_tool_audit_context(agent="kronos", session_id="chat", thread_id="chat")
    try:
        call = asyncio.create_task(BudgetedModel(inner, label="standard", lite_factory=lambda: inner).ainvoke([]))
        await asyncio.to_thread(entered.wait, 5)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if get_swarm().budget_holds()["held"] == 0 and release.is_set():
                break
    finally:
        reset_tool_audit_context(token)

    inner.ainvoke.assert_not_awaited()
    await asyncio.sleep(0.1)
    assert get_swarm().budget_holds() == {"held": 0.0, "unknown": 0.0}
