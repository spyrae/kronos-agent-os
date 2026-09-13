"""Mem0's own workers and SDK must not escape budget or caller attribution."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos import graph, llm
from kronos.audit import get_tool_audit_context, reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings
from kronos.engine import AgentResult
from kronos.execution_control import ExecutionStoppedError, execution_scope
from kronos.memory import store
from kronos.security.cost_guardian import get_guardian
from kronos.security.cost_tracking import record_llm_cost
from kronos.security.model_budget import ModelBudgetError, model_budget_scope
from kronos.swarm_store import get_swarm
from tests.test_cost_tracking import cost_env  # noqa: F401
from tests.test_graph_contract import agent, session_store  # noqa: F401
from tests.test_model_budget import models  # noqa: F401


@pytest.fixture
def memory(request, monkeypatch):
    import sys

    fixture = request.getfixturevalue("models")
    state = SimpleNamespace(calls=[], constructed=[], failure=None, parse_error=False, result='{"facts": ["synthetic fact"]}')

    def completion(**kwargs):
        state.calls.append((kwargs, get_tool_audit_context()))
        if state.failure:
            state.failure()
        message = SimpleNamespace(content=state.result, tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )

    class Provider:
        def __init__(self):
            self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))

        def generate_response(self, messages, response_format=None, tools=None, tool_choice="auto", **kwargs):
            payload = dict(model="deepseek-chat", messages=messages, temperature=0.2, max_tokens=2000, **kwargs)
            if response_format:
                payload["response_format"] = response_format
            if tools:
                payload.update(tools=tools, tool_choice=tool_choice)
            response = self.client.chat.completions.create(**payload)
            if state.parse_error:
                raise RuntimeError("synthetic parser failure")
            message = response.choices[0].message
            if tools:
                return {
                    "content": message.content,
                    "tool_calls": [
                        {"name": call.function.name, "arguments": json.loads(call.function.arguments)}
                        for call in message.tool_calls or []
                    ],
                }
            return message.content

    class Memory:
        def __init__(self):
            self.llm = Provider()
            self.enable_graph = False
            self.shared_data = []
            state.instance = self

        @classmethod
        def from_config(cls, config):
            state.constructed.append(config)
            return cls()

        def add(self, messages, **kwargs):
            # Older Mem0 releases use their own executor without copy_context.
            with ThreadPoolExecutor(max_workers=1) as executor:
                answer = executor.submit(
                    self.llm.generate_response, messages, response_format={"type": "json_object"}
                ).result()
            self.shared_data.append(answer)
            return {"results": [{"memory": answer}]}

        def search(self, query, **kwargs):
            return {"results": []}

        def get_all(self, **kwargs):
            return {"results": []}

    monkeypatch.setitem(sys.modules, "mem0", SimpleNamespace(Memory=Memory))
    monkeypatch.setattr(settings, "deepseek_api_key", "test-only-key")
    store.get_memory.cache_clear()
    state.models = fixture
    yield state
    store.get_memory.cache_clear()


def test_mem0_sdk_usage_reaches_original_scope_across_its_worker(memory):
    token = set_tool_audit_context(agent="nexus", thread_id="chat:8", session_id="chat", user_id="user")
    try:
        result = store.get_memory().add([{"role": "user", "content": "fact"}], user_id="user")
    finally:
        reset_tool_audit_context(token)
    assert result["results"] and len(memory.calls) == 1
    assert memory.calls[0][1]["session_id"] == "chat"
    assert get_swarm().daily_cost()["requests"] == 1
    assert get_guardian()._session_costs["chat"] > 0
    assert memory.instance.shared_data == [memory.result]


@pytest.mark.parametrize("scope", ["daily", "session"])
def test_cached_mem0_checks_budget_on_each_actual_dispatch(memory, scope):
    instance = store.get_memory()
    token = set_tool_audit_context(session_id="chat")
    try:
        if scope == "daily":
            get_swarm().add_cost(agent="nexus", cost_usd=6)
        else:
            get_guardian().record_cost("chat", 1.1)
        with pytest.raises(ModelBudgetError):
            instance.add([{"role": "user", "content": "fact"}], user_id="user")
    finally:
        reset_tool_audit_context(token)
    assert not memory.calls


def test_mem0_soft_downgrade_uses_factory_lite_and_preserves_json_options(memory):
    get_swarm().add_cost(agent="nexus", cost_usd=4.1)
    memory.models.responses["lite"] = AIMessage(content='{"facts": ["lite fact"]}')
    result = store.get_memory().add([{"role": "user", "content": "fact"}], user_id="user")
    assert result["results"][0]["memory"] == '{"facts": ["lite fact"]}'
    assert not memory.calls
    assert memory.models.attempts[0][0] == "lite"
    assert memory.models.attempts[0][3]["response_format"] == {"type": "json_object"}
    assert memory.models.attempts[0][3]["max_tokens"] == 2000


def test_mem0_lite_tool_calls_keep_the_original_parser_contract(memory):
    from kronos.memory.model_boundary import ScopedMemoryLlm

    memory.models.responses["lite"] = AIMessage(
        content="", tool_calls=[{"name": "remember", "args": {"fact": "synthetic"}, "id": "call-1"}]
    )
    tools = [{"type": "function", "function": {"name": "remember", "parameters": {"type": "object"}}}]
    with model_budget_scope("lite"):
        bound = ScopedMemoryLlm(store.get_memory()._memory.llm)
    result = bound.generate_response([{"role": "user", "content": "fact"}], tools=tools, tool_choice="required")
    assert result == {"content": "", "tool_calls": [{"name": "remember", "arguments": {"fact": "synthetic"}}]}
    assert memory.models.attempts[0][1] == tools
    assert memory.models.attempts[0][2] == {"tool_choice": "required"}
    assert not memory.calls


def test_mem0_worker_preserves_execution_stop(memory):
    def stop():
        raise ExecutionStoppedError("synthetic stop")

    with execution_scope(stop), pytest.raises(ExecutionStoppedError):
        store.get_memory().add([{"role": "user", "content": "fact"}], user_id="user")
    assert not memory.calls


def test_mem0_completed_response_is_accounted_before_its_parser_can_fail(memory):
    memory.parse_error = True
    with pytest.raises(RuntimeError, match="synthetic parser failure"):
        store.get_memory().add([{"role": "user", "content": "fact"}], user_id="user")
    assert get_swarm().daily_cost()["requests"] == 1


async def test_shared_mem0_does_not_mix_simultaneous_caller_scopes(memory):
    instance = store.get_memory()

    async def call(session):
        token = set_tool_audit_context(session_id=session)
        try:
            await asyncio.to_thread(instance.add, [{"role": "user", "content": session}], user_id=session)
        finally:
            reset_tool_audit_context(token)

    await asyncio.gather(call("first"), call("second"))
    assert {context["session_id"] for _, context in memory.calls} == {"first", "second"}
    assert set(get_guardian()._session_costs) == {"first", "second"}
    assert get_swarm().daily_cost()["requests"] == 2


def test_no_deepseek_key_does_not_create_implicit_default_api_provider(memory, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    with pytest.raises(RuntimeError, match="DeepSeek.*not configured"):
        store.get_memory()
    assert not memory.constructed


@pytest.mark.parametrize("operation", ["search", "get_all"])
def test_non_llm_mem0_reads_are_not_blocked_by_spend(memory, operation):
    get_swarm().add_cost(agent="nexus", cost_usd=6)
    method = getattr(store.get_memory(), operation)
    args = ("query",) if operation == "search" else ()
    assert method(*args, user_id="user") == {"results": []}
    assert not memory.calls


async def test_full_invocation_context_covers_retrieval_background_and_compaction(request, monkeypatch):
    request.getfixturevalue("models")
    current_agent = request.getfixturevalue("agent")
    observations = []
    stored = asyncio.Event()
    loop = asyncio.get_running_loop()
    current_agent._memory_enabled = True

    def observe(label):
        observations.append((label, get_tool_audit_context(), llm.get_model().invoke([HumanMessage(content="probe")]).content))
        record_llm_cost("test-model", 1, 1, 0.01)

    def retrieve(state):
        observe("retrieve")
        return {}

    def background(state):
        try:
            observe("store")
        finally:
            loop.call_soon_threadsafe(stored.set)
        return {}

    class ContextEngine:
        def should_compact(self, state):
            return True

        def compact(self, state):
            observe("compact")
            return {}

    monkeypatch.setattr(graph, "retrieve_memories", retrieve)
    monkeypatch.setattr(graph, "store_memories_background", background)
    monkeypatch.setattr(graph, "get_context_engine", lambda: ContextEngine())
    monkeypatch.setattr(graph, "react_loop", AsyncMock(return_value=AgentResult(content="answer", messages=[])))
    before = get_tool_audit_context()
    assert await current_agent.ainvoke("hello", "thread", user_id="user", session_id="session", force_tier="lite") == "answer"
    await asyncio.wait_for(stored.wait(), timeout=2)
    assert {label for label, _, _ in observations} == {"retrieve", "store", "compact"}
    assert all(context["session_id"] == "session" for _, context, _ in observations)
    assert all(model == "lite" for _, _, model in observations)
    assert get_guardian()._session_costs["session"] == pytest.approx(0.03)
    assert get_tool_audit_context() == before


def test_multiple_calls_inside_one_mem0_operation_recheck_budget(memory):
    from kronos.memory.model_boundary import ScopedMemoryLlm

    scoped = ScopedMemoryLlm(store.get_memory()._memory.llm)
    memory.failure = lambda: get_swarm().add_cost(agent="nexus", cost_usd=6)
    scoped.generate_response([{"role": "user", "content": "first pass"}])
    with pytest.raises(ModelBudgetError):
        scoped.generate_response([{"role": "user", "content": "second pass"}])
    assert len(memory.calls) == 1


def test_same_operation_supports_concurrent_internal_workers(memory):
    from kronos.memory.model_boundary import ScopedMemoryLlm

    barrier = threading.Barrier(2)
    memory.failure = lambda: barrier.wait(timeout=2)
    token = set_tool_audit_context(session_id="shared-operation")
    try:
        scoped = ScopedMemoryLlm(store.get_memory()._memory.llm)
    finally:
        reset_tool_audit_context(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(scoped.generate_response, [{"role": "user", "content": "pass"}]) for _ in range(2)]
        assert [future.result(timeout=3) for future in futures] == [memory.result, memory.result]
    assert all(context["session_id"] == "shared-operation" for _, context in memory.calls)


@pytest.mark.parametrize("unsupported", ["adapter", "graph", "reranker"])
def test_unknown_mem0_capability_is_not_left_unguarded(memory, unsupported):
    from kronos.memory.model_boundary import BudgetedMemory

    store.get_memory()
    if unsupported == "adapter":
        memory.instance.llm = SimpleNamespace(generate_response=lambda *args: "unguarded")
    elif unsupported == "graph":
        memory.instance.enable_graph = True
    else:
        memory.instance.reranker = object()
    with pytest.raises(RuntimeError, match="budget boundary"):
        BudgetedMemory(memory.instance)


def test_new_mem0_operations_are_not_silently_forwarded(memory):
    instance = store.get_memory()
    memory.instance.delete = lambda *args: "not admitted"
    with pytest.raises(AttributeError):
        instance.delete("id")


def test_lite_mem0_counts_factory_usage_exactly_once(memory, monkeypatch):
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    from kronos.security.cost_tracking import get_cost_callbacks

    class LocalModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "local-test"

        def _generate(self, messages, **kwargs):
            message = AIMessage(content='{"facts": []}', usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
            return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(
        llm._state, "get_or_create", lambda provider: LocalModel(callbacks=get_cost_callbacks(model="deepseek-chat"))
    )
    with model_budget_scope("lite"):
        store.get_memory().add([{"role": "user", "content": "pass"}], user_id="user")
    cost = get_swarm().daily_cost()
    assert (cost["requests"], cost["input_tokens"], cost["output_tokens"]) == (1, 100, 20)
    assert cost["cost_usd"] > 0 and not memory.calls
