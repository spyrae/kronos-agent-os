"""Pluggable Context Engine — strategies for managing conversation context.

Strategies:
- summarize: LLM summarization of old messages (current default)
- sliding_window: simple truncation, keep last N messages (no LLM cost)
- hybrid: sliding window + periodic summarization flushes to long-term memory

Usage in graph.py:
    engine = get_context_engine("summarize")
    if engine.should_compact(state):
        return engine.compact(state)

**Size, not just count.** Every threshold here used to be a number of messages,
which is the wrong unit: five turns carrying a pasted document each are 57k
tokens and would never compact, while sixteen one-line exchanges are under a
thousand and would — paying for a summarisation that saves nothing. Both
triggers now apply, and the size one is the one that matters.
"""

import logging
from abc import ABC, abstractmethod

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from kronos.audit import estimate_tokens
from kronos.state import AgentState

log = logging.getLogger("kronos.memory.context_engine")

# How large the *persisted history* may get before it is compacted. Not the
# model's context window: the working set adds the system prompt, retrieved
# memories and a turn's tool results on top of this.
DEFAULT_TOKEN_BUDGET = 12_000
# A history below budget/this is too small to be worth a summarisation call.
MIN_COMPACT_FRACTION = 4


def message_text(message) -> str:
    content = getattr(message, "content", message)
    return content if isinstance(content, str) else str(content)


def history_tokens(messages: list) -> int:
    """What this history costs to send, near enough to budget with."""
    return sum(estimate_tokens(message_text(message)) for message in messages)


def token_budget() -> int:
    """The configured history budget, or the default."""
    from kronos.config import settings

    configured = int(getattr(settings, "context_token_budget", 0) or 0)
    return configured if configured > 0 else DEFAULT_TOKEN_BUDGET


def over_budget(messages: list) -> bool:
    return history_tokens(messages) > token_budget()


def worth_compacting(messages: list, count_limit: int) -> bool:
    """Whether to compact: over budget, or long enough that it is worth doing.

    The count trigger carries a floor because compacting is not free — the
    summarising engine spends a model call. Thirty-two one-line exchanges are a
    thousand tokens; summarising them costs more than carrying them, and the old
    count-only rule did exactly that.
    """
    if over_budget(messages):
        return True
    if len(messages) <= count_limit:
        return False
    return history_tokens(messages) >= token_budget() // MIN_COMPACT_FRACTION


def trim_to_budget(messages: list, budget: int | None = None) -> list:
    """Keep the newest messages that fit. Never returns nothing.

    Used when even the sliding window is too large to send — a window counted in
    messages says nothing about the size of those messages.
    """
    limit = budget if budget is not None else token_budget()
    kept: list = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(message_text(message))
        if kept and used + cost > limit:
            break
        kept.append(message)
        used += cost
    return list(reversed(kept))


class ContextEngine(ABC):
    """Abstract context management strategy."""

    @abstractmethod
    def should_compact(self, state: AgentState) -> bool:
        """Check if conversation needs compaction."""

    @abstractmethod
    def compact(self, state: AgentState) -> AgentState:
        """Compact the conversation. Returns state update dict."""

    def assemble(self, state: AgentState, memories: list[str]) -> list:
        """Assemble context before LLM call (optional override).

        Default: inject memories as SystemMessage before conversation.
        """
        if not memories:
            return []

        memory_text = "\n".join(f"- {m}" for m in memories)
        return [SystemMessage(content=f"[Relevant memories]\n{memory_text}")]


class SummarizeEngine(ContextEngine):
    """LLM-based summarization. Preserves critical identifiers.

    Best for: complex multi-turn conversations where context matters.
    Cost: ~$0.01 per compaction (DeepSeek lite).
    """

    def __init__(self, max_messages: int = 30, keep_recent: int = 6):
        self.max_messages = max_messages
        self.keep_recent = keep_recent

    def should_compact(self, state: AgentState) -> bool:
        return worth_compacting(state.get("messages", []), self.max_messages)

    def compact(self, state: AgentState) -> AgentState:
        # Import here to avoid circular deps
        from kronos.memory.compaction import compact_messages

        return compact_messages(state)


class SlidingWindowEngine(ContextEngine):
    """Simple truncation — keep last N messages, drop older ones.

    Best for: casual conversations, low-cost mode.
    Cost: $0 (no LLM calls).
    """

    def __init__(self, window_size: int = 20):
        self.window_size = window_size

    def should_compact(self, state: AgentState) -> bool:
        return worth_compacting(state.get("messages", []), self.window_size)

    def compact(self, state: AgentState) -> AgentState:
        messages = state.get("messages", [])
        if len(messages) <= self.window_size and not over_budget(messages):
            return {}

        kept = list(messages[-self.window_size :])
        # A window counted in messages says nothing about their size, so the
        # window itself gets trimmed to the budget.
        kept = trim_to_budget(kept)
        dropped = len(messages) - len(kept)

        log.info("Sliding window: dropped %d messages, kept %d (%d tokens)", dropped, len(kept), history_tokens(kept))

        return {"messages": kept}


class HybridEngine(ContextEngine):
    """Sliding window + periodic memory flush.

    Keeps a sliding window of recent messages (no LLM cost per turn).
    When dropping messages, flushes them to Mem0 long-term memory
    so facts aren't permanently lost.

    Best for: balanced cost/quality, personal assistant use case.
    Cost: ~$0.005 per flush (Mem0 extraction only, no summarization).
    """

    def __init__(self, window_size: int = 24, flush_threshold: int = 30):
        self.window_size = window_size
        self.flush_threshold = flush_threshold

    def should_compact(self, state: AgentState) -> bool:
        return worth_compacting(state.get("messages", []), self.flush_threshold)

    def compact(self, state: AgentState) -> AgentState:
        messages = state.get("messages", [])
        if len(messages) <= self.flush_threshold and not over_budget(messages):
            return {}

        user_id = state.get("user_id", "")
        session_id = state.get("session_id", "")

        # What to keep is decided by size first: a few very large messages must
        # be dropped even when there are fewer of them than the window allows.
        to_keep = trim_to_budget(messages[-self.window_size :])
        drop_count = len(messages) - len(to_keep)
        to_flush = messages[:drop_count]

        # Flush dropped messages to long-term memory
        if user_id and to_flush:
            try:
                from kronos.memory.store import add_memories

                flush_pairs = []
                for msg in to_flush:
                    if isinstance(msg, HumanMessage):
                        content = msg.content if isinstance(msg.content, str) else str(msg.content)
                        flush_pairs.append({"role": "user", "content": content})
                    elif isinstance(msg, AIMessage):
                        content = msg.content if isinstance(msg.content, str) else str(msg.content)
                        if content:
                            flush_pairs.append({"role": "assistant", "content": content})

                if flush_pairs:
                    add_memories(flush_pairs, user_id, session_id)
                    log.info("Hybrid flush: %d messages → long-term memory", len(flush_pairs))
            except Exception as e:
                log.error("Hybrid memory flush failed: %s", e)

        # Add a marker so agent knows context was truncated
        marker = SystemMessage(
            content=f"[Context window: {drop_count} older messages moved to long-term memory. "
            f"Use load_skill or memory search if you need earlier context.]"
        )

        log.info("Hybrid compact: %d dropped → memory, %d kept", drop_count, len(to_keep))
        return {"messages": [marker] + to_keep}


# --- Registry ---

_ENGINES: dict[str, type[ContextEngine]] = {
    "summarize": SummarizeEngine,
    "sliding_window": SlidingWindowEngine,
    "hybrid": HybridEngine,
}

_active_engine: ContextEngine | None = None


def get_context_engine(strategy: str = "summarize", **kwargs) -> ContextEngine:
    """Get or create the active context engine.

    Args:
        strategy: "summarize", "sliding_window", or "hybrid".
        **kwargs: Strategy-specific params (max_messages, window_size, etc.).
    """
    global _active_engine

    if _active_engine is None or type(_active_engine).__name__.lower().replace("engine", "") != strategy:
        engine_cls = _ENGINES.get(strategy)
        if not engine_cls:
            log.warning("Unknown strategy '%s', falling back to summarize", strategy)
            engine_cls = SummarizeEngine
        _active_engine = engine_cls(**kwargs)
        log.info("Context engine: %s (%s)", strategy, type(_active_engine).__name__)

    return _active_engine
