"""Independent tool calls run together (gap #1).

A model asking for several lookups in one message is the ordinary shape of
research work — "check both marketplaces", "read all three sources". Running
them one after another makes the turn cost the sum instead of the slowest call.

The interesting tests here are the negative ones. Concurrency is only safe for
calls that cannot observe each other, and the sequential path is load bearing:
an approval pauses the turn and defers the rest, a side effect is written to a
ledger, and a delegating tool reads a context variable set immediately before it
runs. Each of those has a test saying "not in parallel".
"""

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

from kronos.config import settings
from kronos.engine import react_loop, tool_delegates, tool_runs_in_parallel
from kronos.security.effects import mark_side_effect


class SlowTool(BaseTool):
    """Records when it started and finished, so overlap is observable."""

    name: str = "search_a"
    description: str = "search source A"
    delay: float = 0.15
    spans: list = []

    def _run(self, **kwargs) -> str:
        raise NotImplementedError

    async def _arun(self, **kwargs) -> str:
        started = time.perf_counter()
        await asyncio.sleep(self.delay)
        self.spans.append((started, time.perf_counter()))
        return f"{self.name} done"


def _overlapping(spans: list[tuple[float, float]]) -> bool:
    """True when any two runs were in flight at the same moment."""
    ordered = sorted(spans)
    return any(later[0] < earlier[1] for earlier, later in zip(ordered, ordered[1:], strict=False))


class ScriptedModel:
    """Emits one tool-calling message, then a final answer."""

    model_name = "scripted"

    def __init__(self, calls: list[dict]):
        self._calls = calls
        self._sent = False

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        if self._sent:
            return AIMessage(content="готово")
        self._sent = True
        return AIMessage(content="", tool_calls=self._calls)


def _call(name: str, call_id: str, **args) -> dict:
    return {"name": name, "args": args or {"q": call_id}, "id": call_id}


@pytest.fixture(autouse=True)
def approvals_off(monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)


# --- eligibility --------------------------------------------------------------


def test_a_plain_tool_may_run_in_parallel():
    assert tool_runs_in_parallel(SlowTool()) is True


def test_a_side_effecting_tool_may_not():
    sender = SlowTool(name="send_message")
    mark_side_effect([sender])

    assert tool_runs_in_parallel(sender) is False


def test_a_delegating_tool_may_not():
    delegate = SlowTool(name="delegate_to_research")
    delegate.metadata = {"delegates": True}

    assert tool_delegates(delegate) is True
    assert tool_runs_in_parallel(delegate) is False


# --- the win ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_independent_lookups_overlap():
    first, second = SlowTool(name="search_a"), SlowTool(name="search_b")
    first.spans, second.spans = [], []
    model = ScriptedModel([_call("search_a", "c1"), _call("search_b", "c2")])

    started = time.perf_counter()
    result = await react_loop(model, [HumanMessage(content="найди в обоих источниках")], [first, second])
    elapsed = time.perf_counter() - started

    assert result.tool_calls_count == 2
    assert elapsed < first.delay + second.delay, "the calls ran one after another"
    assert _overlapping(first.spans + second.spans)


@pytest.mark.asyncio
async def test_results_keep_the_order_the_model_asked_for(monkeypatch):
    """Concurrency must not reorder the ToolMessages the model reads back."""
    fast = SlowTool(name="search_fast", delay=0.01)
    slow = SlowTool(name="search_slow", delay=0.12)
    fast.spans, slow.spans = [], []
    model = ScriptedModel([_call("search_slow", "c1"), _call("search_fast", "c2")])

    result = await react_loop(model, [HumanMessage(content="оба источника")], [slow, fast])

    tool_messages = [m for m in result.messages if m.type == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert "search_slow" in str(tool_messages[0].content)


@pytest.mark.asyncio
async def test_a_single_call_is_not_treated_as_a_batch():
    only = SlowTool(name="search_a")
    only.spans = []
    model = ScriptedModel([_call("search_a", "c1")])

    result = await react_loop(model, [HumanMessage(content="один источник")], [only])

    assert result.tool_calls_count == 1
    assert len(only.spans) == 1


# --- what must stay sequential ------------------------------------------------


@pytest.mark.asyncio
async def test_side_effecting_calls_do_not_overlap():
    """Two sends must not race: the ledger and the outside world are ordered."""
    first, second = SlowTool(name="send_one"), SlowTool(name="send_two")
    first.spans, second.spans = [], []
    mark_side_effect([first, second])
    model = ScriptedModel([_call("send_one", "c1"), _call("send_two", "c2")])

    await react_loop(model, [HumanMessage(content="отправь оба")], [first, second])

    assert not _overlapping(first.spans + second.spans)


@pytest.mark.asyncio
async def test_a_delegating_call_does_not_overlap_with_a_lookup():
    """A delegation tool reads a context variable set right before it runs."""
    lookup = SlowTool(name="search_a")
    delegate = SlowTool(name="delegate_to_research")
    delegate.metadata = {"delegates": True}
    lookup.spans, delegate.spans = [], []
    model = ScriptedModel([_call("delegate_to_research", "c1"), _call("search_a", "c2")])

    await react_loop(model, [HumanMessage(content="делегируй и поищи")], [delegate, lookup])

    assert not _overlapping(lookup.spans + delegate.spans)


@pytest.mark.asyncio
async def test_a_call_awaiting_approval_never_starts_early(monkeypatch):
    """The whole point of an approval is that the tool has not run yet."""
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    risky = SlowTool(name="deploy_thing")
    safe = SlowTool(name="search_a")
    risky.spans, safe.spans = [], []

    async def approve(tool, tc):
        return "approval-1"

    model = ScriptedModel([_call("deploy_thing", "c1"), _call("search_a", "c2")])

    result = await react_loop(
        model,
        [HumanMessage(content="выкати и поищи")],
        [risky, safe],
        request_tool_approval=approve,
    )

    assert result.waiting_approval is True
    assert risky.spans == [], "the approval-gated tool must not have run"


@pytest.mark.asyncio
async def test_identical_calls_are_not_duplicated_concurrently():
    """Same tool, same args: one remote rate limit, one idempotency key."""
    tool = SlowTool(name="search_a")
    tool.spans = []
    model = ScriptedModel([_call("search_a", "c1", q="ROG Ally"), _call("search_a", "c2", q="ROG Ally")])

    await react_loop(model, [HumanMessage(content="дважды одно и то же")], [tool])

    assert not _overlapping(tool.spans), "identical calls must not race each other"


@pytest.mark.asyncio
async def test_a_memoized_result_is_not_recomputed():
    """A resumed turn must reuse what the crashed one already paid for."""
    tool = SlowTool(name="search_a")
    other = SlowTool(name="search_b")
    tool.spans, other.spans = [], []
    model = ScriptedModel([_call("search_a", "c1"), _call("search_b", "c2")])

    async def cached(call_id: str):
        return "из кэша" if call_id == "c1" else None

    result = await react_loop(
        model,
        [HumanMessage(content="оба")],
        [tool, other],
        get_cached_tool_result=cached,
    )

    assert tool.spans == [], "the memoized call must not run again"
    assert any("из кэша" in str(m.content) for m in result.messages if m.type == "tool")


@pytest.mark.asyncio
async def test_an_unknown_tool_still_reports_normally():
    known = SlowTool(name="search_a")
    known.spans = []
    model = ScriptedModel([_call("search_a", "c1"), _call("nope", "c2")])

    result = await react_loop(model, [HumanMessage(content="один есть, второго нет")], [known])

    errors = [m for m in result.messages if m.type == "tool" and "Unknown tool" in str(m.content)]
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_one_failing_lookup_does_not_sink_the_others():
    class Broken(BaseTool):
        name: str = "search_broken"
        description: str = "always fails"

        def _run(self, **kwargs) -> str:
            raise RuntimeError("upstream down")

    working = SlowTool(name="search_a")
    working.spans = []
    model = ScriptedModel([_call("search_broken", "c1"), _call("search_a", "c2")])

    result = await react_loop(model, [HumanMessage(content="оба источника")], [Broken(), working])

    contents = [str(m.content) for m in result.messages if m.type == "tool"]
    assert any("[ERROR]" in c for c in contents)
    assert any("search_a done" in c for c in contents)


@pytest.mark.asyncio
async def test_an_approval_pause_leaves_a_well_formed_history(monkeypatch):
    """Two lookups do run in parallel here; the gated call still must not.

    Every tool_call in the assistant message needs a ToolMessage or the next LLM
    call fails on protocol. The lookups' real results are discarded in favour of
    deferred placeholders — which is why only read-only calls are allowed to run
    ahead in the first place.
    """
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    risky = SlowTool(name="deploy_thing")
    one, two = SlowTool(name="search_a"), SlowTool(name="search_b")
    risky.spans, one.spans, two.spans = [], [], []

    async def approve(tool, tc):
        return "approval-1"

    model = ScriptedModel([_call("deploy_thing", "c1"), _call("search_a", "c2"), _call("search_b", "c3")])

    result = await react_loop(
        model,
        [HumanMessage(content="выкати и поищи в двух местах")],
        [risky, one, two],
        request_tool_approval=approve,
    )

    assert result.waiting_approval is True
    assert risky.spans == [], "the gated tool must not have run"
    answered = {m.tool_call_id for m in result.messages if m.type == "tool"}
    assert answered == {"c2", "c3"}, "every call except the awaited one is answered"
