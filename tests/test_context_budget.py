"""Context management measured in tokens, which is the unit that costs money.

Every threshold here used to be a number of messages. That is backwards in both
directions at once: five turns carrying a pasted document each are 57k tokens
and never compacted, while sixteen one-line exchanges are under a thousand and
did — paying for a summarisation that saved nothing.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos.config import settings
from kronos.memory.context_engine import (
    DEFAULT_TOKEN_BUDGET,
    HybridEngine,
    SlidingWindowEngine,
    SummarizeEngine,
    history_tokens,
    over_budget,
    token_budget,
    trim_to_budget,
)


def _history(turns: int, size: int) -> list:
    messages = []
    for _ in range(turns):
        messages.append(HumanMessage(content="x" * size))
        messages.append(AIMessage(content="ok"))
    return messages


HUGE = _history(5, 40_000)  # 10 messages, ~57k tokens
CHATTY = _history(16, 200)  # 32 messages, ~1k tokens


# --- measuring ----------------------------------------------------------------


def test_history_is_measured_in_tokens():
    assert history_tokens(_history(1, 3500)) > 1000
    assert history_tokens([]) == 0


def test_the_budget_is_configurable_and_has_a_default(monkeypatch):
    assert token_budget() == DEFAULT_TOKEN_BUDGET

    monkeypatch.setattr(settings, "context_token_budget", 500)
    assert token_budget() == 500

    monkeypatch.setattr(settings, "context_token_budget", 0)
    assert token_budget() == DEFAULT_TOKEN_BUDGET, "0 means 'use the default', not 'no history'"


def test_over_budget_looks_at_size_not_count():
    assert over_budget(HUGE) is True, "10 huge messages are the case that used to slip through"
    assert over_budget(CHATTY) is False, "32 tiny messages are not a context problem"


# --- trimming -----------------------------------------------------------------


def test_trimming_keeps_the_newest_that_fit():
    messages = [HumanMessage(content="x" * 3500) for _ in range(10)]

    kept = trim_to_budget(messages, budget=3000)

    assert len(kept) == 3
    assert kept[-1] is messages[-1], "the newest message is the one that must survive"


def test_trimming_never_returns_nothing():
    """A single message over budget still has to be sent — dropping it loses the ask."""
    kept = trim_to_budget([HumanMessage(content="x" * 100_000)], budget=10)

    assert len(kept) == 1


# --- the three engines --------------------------------------------------------


@pytest.mark.parametrize("engine", [SummarizeEngine(), SlidingWindowEngine(), HybridEngine()])
def test_every_engine_compacts_a_large_history(engine):
    assert engine.should_compact({"messages": HUGE}) is True


def test_a_long_but_tiny_history_is_not_worth_a_model_call():
    """Thirty-two one-line exchanges are a thousand tokens; summarising costs more."""
    assert SummarizeEngine().should_compact({"messages": CHATTY}) is False


def test_the_count_trigger_still_fires_once_there_is_something_to_gain():
    """It is what keeps a long thread from creeping up on the budget."""
    long_enough = _history(20, 700)  # 40 messages, several thousand tokens

    assert len(long_enough) > 30
    assert SummarizeEngine().should_compact({"messages": long_enough}) is True


def test_a_short_small_history_is_left_alone():
    assert SummarizeEngine().should_compact({"messages": _history(3, 200)}) is False
    assert SlidingWindowEngine().should_compact({"messages": _history(3, 200)}) is False
    assert HybridEngine().should_compact({"messages": _history(3, 200)}) is False


def test_the_sliding_window_trims_by_size_too():
    """Twenty messages is a window; twenty huge messages is still too much to send."""
    result = SlidingWindowEngine(window_size=20).compact({"messages": HUGE})

    assert result["messages"], "something must survive"
    assert history_tokens(result["messages"]) <= token_budget()


def test_the_hybrid_engine_keeps_what_fits_and_flushes_the_rest(monkeypatch):
    flushed: list = []
    monkeypatch.setattr(
        "kronos.memory.store.add_memories",
        lambda pairs, user_id, session_id: flushed.extend(pairs),
    )

    result = HybridEngine().compact({"messages": HUGE, "user_id": "u1", "session_id": "s1"})

    assert history_tokens(result["messages"]) <= token_budget() + 200, "the marker adds a little"
    assert flushed, "what was dropped goes to long-term memory rather than vanishing"


def test_a_memory_flush_that_fails_does_not_lose_the_compaction(monkeypatch):
    def broken(pairs, user_id, session_id):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr("kronos.memory.store.add_memories", broken)

    result = HybridEngine().compact({"messages": HUGE, "user_id": "u1", "session_id": "s1"})

    assert history_tokens(result["messages"]) <= token_budget() + 200


# --- the summarising path -----------------------------------------------------


def test_compaction_keeps_a_tail_that_actually_fits(monkeypatch):
    """Six recent messages carrying a document each are still over budget."""
    from kronos.memory import compaction

    monkeypatch.setattr(compaction, "_chunk_summarize", lambda text: "summary")
    monkeypatch.setattr(compaction, "add_memories", lambda *a, **k: None)

    result = compaction.compact_messages({"messages": HUGE, "user_id": "", "session_id": ""})

    assert history_tokens(result["messages"]) <= token_budget()


def test_the_standalone_trigger_agrees_with_the_engines():
    from kronos.memory.compaction import should_compact

    assert should_compact({"messages": HUGE}) is True
    assert should_compact({"messages": _history(3, 200)}) is False
