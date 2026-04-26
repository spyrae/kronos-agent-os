"""ASO Pipeline Graph — full LangGraph definition.

Flow:
    START → monitor → analyze → decide ─┬─→ notify → END  (nothing to do)
                                         └─→ plan ─→ review ─┬─→ execute → wait → measure → evaluate → notify → END
                                               ▲              ├─→ plan  (reject + feedback → revision)
                                               │              └─→ notify → END  (skip)
                                               │
                                               └── (revision loop, max 3 iterations)
"""

from __future__ import annotations

import logging

from langgraph.graph import StateGraph, START, END

from .state import ASOState
from .nodes.monitor import monitor
from .nodes.analyze import analyze
from .nodes.decide import decide
from .nodes.plan import plan
from .nodes.review import review
from .nodes.execute import execute
from .nodes.wait import wait
from .nodes.measure import measure
from .nodes.evaluate import evaluate
from .nodes.notify import notify

log = logging.getLogger("aso.graph")


def _route_after_decide(state: ASOState) -> str:
    """Route: if actionable opportunity found → plan, else → notify."""
    if state.get("selected_opportunity"):
        return "plan"
    return "notify"


def build_graph() -> StateGraph:
    """Build the full ASO pipeline graph.

    Nodes:
        monitor  — collect data (no LLM)
        analyze  — find opportunities (LLM)
        decide   — select best opportunity (rule-based)
        plan     — generate changes (LLM)
        review   — human approval (interrupt)
        execute  — apply via API
        wait     — pause N days (interrupt)
        measure  — collect post-metrics
        evaluate — assess impact (LLM)
        notify   — send to Telegram

    review returns Command with goto, so edges from review
    are handled by Command routing, not conditional_edges.
    """
    builder = StateGraph(ASOState)

    # --- Nodes ---
    builder.add_node("monitor", monitor)
    builder.add_node("analyze", analyze)
    builder.add_node("decide", decide)
    builder.add_node("plan", plan)
    builder.add_node("review", review)
    builder.add_node("execute", execute)
    builder.add_node("wait", wait)
    builder.add_node("measure", measure)
    builder.add_node("evaluate", evaluate)
    builder.add_node("notify", notify)

    # --- Edges ---

    # Linear: START → monitor → analyze → decide
    builder.add_edge(START, "monitor")
    builder.add_edge("monitor", "analyze")
    builder.add_edge("analyze", "decide")

    # Branch: decide → plan (action) or notify (no action)
    builder.add_conditional_edges("decide", _route_after_decide, ["plan", "notify"])

    # plan → review (always)
    builder.add_edge("plan", "review")

    # review → Command routing (approve→execute, reject→plan, skip→notify)
    # No explicit edges needed — review() returns Command with goto

    # execute → wait → measure → evaluate → notify
    builder.add_edge("execute", "wait")
    builder.add_edge("wait", "measure")
    builder.add_edge("measure", "evaluate")
    builder.add_edge("evaluate", "notify")

    # notify → END (terminal for all paths)
    builder.add_edge("notify", END)

    return builder


def compile_graph(*, sqlite_path: str = "aso_checkpoints.db"):
    """Compile graph with SQLite checkpointer for persistence."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    checkpointer = AsyncSqliteSaver.from_conn_string(sqlite_path)
    builder = build_graph()
    return builder.compile(checkpointer=checkpointer)
