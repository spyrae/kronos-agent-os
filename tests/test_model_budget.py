"""Real factory admission with fake providers, including cached supervisors."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos import llm
from kronos.audit import reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings
from kronos.execution_control import ExecutionStoppedError, execution_scope
from kronos.security.model_budget import ModelBudgetError, model_budget_scope
from kronos.swarm_store import get_swarm
from tests.test_cost_tracking import cost_env  # noqa: F401


@pytest.fixture
def models(request, monkeypatch):
    request.getfixturevalue("cost_env")
    workspace = Path(settings.db_dir) / "workspace"
    (workspace / "self" / "skills").mkdir(parents=True)
    monkeypatch.setattr(settings, "workspace_path", str(workspace))
    attempts, failures, responses = [], {}, {}

    class Provider:
        def __init__(self, name, tools=None, tool_options=None):
            self.model = name
            self.tools = tools
            self.tool_options = tool_options

        def bind_tools(self, tools, **kwargs):
            return Provider(self.model, tools, kwargs)

        def invoke(self, messages, **kwargs):
            attempts.append((self.model, self.tools, self.tool_options, kwargs))
            if self.model in failures:
                failures[self.model]()
            return responses.get(self.model, AIMessage(content=self.model))

        async def ainvoke(self, messages, **kwargs):
            await asyncio.sleep(0)
            return self.invoke(messages, **kwargs)

        def stream(self, messages):
            raise AssertionError("unguarded stream escaped")

    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "standard")
    monkeypatch.setattr(settings, "kaos_lite_provider_chain", "lite")
    monkeypatch.setattr(settings, "kaos_orchestrator_provider_chain", "orchestrator")
    monkeypatch.setattr(llm, "_has_key", lambda provider: provider in {"standard", "lite", "orchestrator", "backup"})
    monkeypatch.setattr(llm._state, "get_or_create", lambda provider: Provider(provider))
    monkeypatch.setattr(llm._state, "_cooldowns", {})
    return SimpleNamespace(attempts=attempts, failures=failures, responses=responses)


@pytest.mark.parametrize("factory", [llm.get_model, llm.get_orchestrator_model, llm.get_fallback_model])
@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
async def test_every_factory_checks_budget_at_invocation_after_construction(models, factory, method):
    model = factory()
    get_swarm().add_cost(agent="another-agent", cost_usd=6)
    with pytest.raises(ModelBudgetError, match="Daily cost limit"):
        if method == "ainvoke":
            await model.ainvoke([HumanMessage(content="hello")])
        else:
            model.invoke([HumanMessage(content="hello")])
    assert not models.attempts


@pytest.mark.parametrize("factory", [llm.get_model, llm.get_orchestrator_model])
@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
async def test_cached_and_tool_bound_models_downgrade_at_call_time(models, factory, method):
    model = factory().bind_tools(["owned-tool"], tool_choice="auto")
    get_swarm().add_cost(agent="another-agent", cost_usd=4.1)
    if method == "ainvoke":
        response = await model.ainvoke([HumanMessage(content="hello")], temperature=0.1)
    else:
        response = model.invoke([HumanMessage(content="hello")], temperature=0.1)
    assert response.content == "lite"
    assert models.attempts == [("lite", ["owned-tool"], {"tool_choice": "auto"}, {"temperature": 0.1})]


async def test_force_lite_is_task_local_and_cannot_be_undone_by_child(models):
    model = llm.get_orchestrator_model()

    async def call(tier):
        with model_budget_scope(tier):
            with model_budget_scope("standard"):
                return (await model.ainvoke([HumanMessage(content="hello")])).content

    assert await asyncio.gather(call("lite"), call("standard")) == ["lite", "orchestrator"]
    assert model.invoke([HumanMessage(content="after reset")]).content == "orchestrator"


@pytest.mark.parametrize("spend,expected", [(6, None), (4.1, "lite")])
@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
async def test_each_fallback_attempt_rechecks_and_never_upgrades(models, monkeypatch, spend, expected, method):
    monkeypatch.setattr(settings, "kaos_standard_provider_chain", "standard,backup")

    def fail():
        get_swarm().add_cost(agent="nexus", cost_usd=spend)
        raise TimeoutError("synthetic timeout after a billable request")

    models.failures["standard"] = fail
    model = llm.get_model().bind_tools(["tool"], tool_choice="auto")

    async def invoke():
        if method == "ainvoke":
            return await model.ainvoke([HumanMessage(content="hello")])
        return model.invoke([HumanMessage(content="hello")])

    if expected is None:
        with pytest.raises(ModelBudgetError, match="Daily cost limit"):
            await invoke()
        assert [item[0] for item in models.attempts] == ["standard"]
    else:
        assert (await invoke()).content == expected
        assert [item[0] for item in models.attempts] == ["standard", "lite"]
        assert models.attempts[-1][1] == ["tool"]
        assert models.attempts[-1][2] == {"tool_choice": "auto"}
    assert "backup" not in llm._state._cooldowns


def test_accounting_read_failure_is_not_free_spend(models, monkeypatch):
    def fail():
        raise OSError("test DB unavailable")

    monkeypatch.setattr(get_swarm(), "daily_cost", fail)
    with pytest.raises(ModelBudgetError, match="accounting unavailable"):
        llm.get_model().invoke([HumanMessage(content="hello")])
    assert not models.attempts


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, None])
def test_invalid_accounting_never_passes_admission(models, monkeypatch, value):
    monkeypatch.setattr(get_swarm(), "daily_cost", lambda: {"cost_usd": value})
    with pytest.raises(ModelBudgetError, match="accounting unavailable"):
        llm.get_model().invoke([HumanMessage(content="hello")])
    assert not models.attempts


def test_session_budget_uses_request_context_outside_telegram(models):
    from kronos.security.cost_guardian import get_guardian

    get_guardian().record_cost("resumed-session", 1.1)
    token = set_tool_audit_context(thread_id="resumed-session")
    try:
        with pytest.raises(ModelBudgetError, match="Session cost limit"):
            llm.get_model().invoke([HumanMessage(content="hello")])
    finally:
        reset_tool_audit_context(token)
    assert not models.attempts


def test_cached_model_also_checks_new_execution_scope(models):
    model = llm.get_model()

    def stop():
        raise ExecutionStoppedError("stopped")

    with execution_scope(stop), pytest.raises(ExecutionStoppedError):
        model.invoke([HumanMessage(content="hello")])
    assert not models.attempts


def test_unhandled_model_methods_cannot_bypass_budget(models):
    with pytest.raises(AttributeError, match="budget admission contract"):
        llm.get_model().stream([HumanMessage(content="hello")])
    assert not models.attempts


def test_budget_exception_is_never_a_provider_retry(models):
    assert not llm.is_retriable_llm_error(ModelBudgetError("accounting timeout, 503"))
    assert not models.attempts


def test_string_lite_tier_is_a_supported_factory_input(models):
    assert llm.get_model("lite").invoke([HumanMessage(content="hello")]).content == "lite"


async def test_graph_supervisor_path_inherits_force_lite(models):
    from kronos.engine import react_loop
    from kronos.graph import KronosAgent

    model = llm.get_orchestrator_model()
    agent = object.__new__(KronosAgent)
    agent._emit_tool_event = lambda *a, **k: None

    async def supervisor(messages, **kwargs):
        return await react_loop(model=model, messages=messages, tools=[], **kwargs)

    agent._supervisor = supervisor
    result = await agent._run_guarded_model_loop(
        thread_id="session",
        turn_id=None,
        messages=[HumanMessage(content="hello")],
        source_message="hello",
        react_loop_kwargs={},
        force_tier="lite",
    )
    assert result.content == "lite"
    assert [item[0] for item in models.attempts] == ["lite"]


async def test_budget_exhaustion_between_react_steps_stops_the_second_model(models):
    from langchain_core.tools import StructuredTool

    from kronos.engine import react_loop

    calls = []

    def read() -> str:
        calls.append("read")
        get_swarm().add_cost(agent="nexus", cost_usd=6)
        return "read result"

    models.responses["standard"] = AIMessage(content="", tool_calls=[{"id": "read-1", "name": "read", "args": {}}])
    result = await react_loop(
        model=llm.get_model(),
        messages=[HumanMessage(content="hello")],
        tools=[StructuredTool.from_function(read, description="Read-only test tool")],
    )
    assert result.failure_reason == "budget_blocked"
    assert "бюджетным контролем" in result.content
    assert calls == ["read"]
    assert [item[0] for item in models.attempts] == ["standard"]


@pytest.mark.parametrize("entry", ["invoke", "resume"])
async def test_real_durable_graph_paths_do_not_complete_when_budget_blocks(models, monkeypatch, entry):
    from kronos.graph import KronosAgent
    from kronos.session import SessionStore

    store = SessionStore(settings.db_path, agent_name=settings.agent_name)
    agent = KronosAgent(tools=[], enable_memory=False, enable_supervisor=False, session_store=store)
    monkeypatch.setattr(agent, "_get_system_prompt", lambda: "Test system prompt")
    get_swarm().add_cost(agent="nexus", cost_usd=6)
    if entry == "invoke":
        outcome = await agent.ainvoke_outcome("hello", thread_id="budget-test", session_id="budget-test")
    else:
        turn_id = await store.begin_turn("budget-test", "hello")
        await agent.resume_interrupted_turn(turn_id, notify=False)
        outcome = await agent.get_turn_outcome(turn_id)
    assert outcome.status == "failed"
    assert outcome.reason == "budget_blocked"
    assert not models.attempts


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf"), True])
def test_invalid_record_cannot_reduce_accounted_spend(models, cost):
    from kronos.security.cost_tracking import record_llm_cost

    get_swarm().add_cost(agent="nexus", cost_usd=1)
    with pytest.raises(ValueError, match="finite and non-negative"):
        record_llm_cost("test", 10, 10, cost)
    assert get_swarm().daily_cost()["cost_usd"] == 1


def test_utc_day_is_shared_for_writer_and_both_readers(models, monkeypatch):
    import time

    import kronos.swarm_store as swarm_module

    fixed = time.struct_time((2026, 9, 8, 0, 1, 0, 1, 251, 0))
    monkeypatch.setattr(swarm_module.time, "gmtime", lambda: fixed)
    swarm = get_swarm()
    swarm.add_cost(agent="nexus", cost_usd=1)
    assert swarm.daily_cost()["date"] == "2026-09-08"
    assert swarm.daily_cost("2026-09-08")["cost_usd"] == 1
    assert swarm.per_agent_daily_cost()["nexus"] == 1


def test_thread_only_context_is_shared_by_recording_and_admission(models):
    from kronos.security.cost_tracking import record_llm_cost

    token = set_tool_audit_context(thread_id="only-thread")
    try:
        record_llm_cost("test", 10, 10, 1.1)
        with pytest.raises(ModelBudgetError, match="Session cost limit"):
            llm.get_model().invoke([HumanMessage(content="hello")])
    finally:
        reset_tool_audit_context(token)
    assert not models.attempts
