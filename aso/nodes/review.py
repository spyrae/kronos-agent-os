"""REVIEW node — human-in-the-loop approval via interrupt().

Pauses the graph and sends the optimization plan to Telegram.
Waits for human input: approve, reject+feedback, or skip.

The graph resumes when the runner calls graph.ainvoke() with
the human's response via Command.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from langgraph.types import interrupt, Command

from ..state import ASOState

log = logging.getLogger("aso.nodes.review")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:8788/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "kronos-cron-2026")


def _send_telegram(text: str) -> None:
    """Send message via Kronos Telegram bridge webhook."""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Secret": WEBHOOK_SECRET,
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)


def _format_plan_for_telegram(state: ASOState) -> str:
    """Format the optimization plan as a readable Telegram message."""
    plan = state.get("optimization_plan", {})
    opp = state.get("selected_opportunity", {})
    cycle_id = state.get("cycle_id", "?")

    lines = [
        f"🎯 ASO Plan — Cycle #{cycle_id}",
        "",
        f"Opportunity: {opp.get('type', '?')} ({opp.get('priority', '?')})",
        f"Locale: {opp.get('locale', '?')}",
        "",
    ]

    # Changes
    changes = plan.get("changes", [])
    if changes:
        lines.append("Изменения:")
        for ch in changes:
            lines.append(f"  {ch.get('field', '?')}:")
            lines.append(f"    было: \"{ch.get('current', '—')}\"")
            lines.append(f"    будет: \"{ch.get('proposed', '—')}\"")
            if ch.get("rationale"):
                lines.append(f"    причина: {ch['rationale']}")
            lines.append("")

    # Impact & Risk
    lines.append(f"Expected: {plan.get('expected_impact', '?')}")
    lines.append(f"Risk: {plan.get('risk_assessment', '?')}")
    lines.append(f"Measurement: {plan.get('measurement_period_days', '?')} days")
    lines.append("")

    # Actions
    lines.append("Команды:")
    lines.append("/aso approve — применить изменения")
    lines.append("/aso reject <комментарий> — на доработку")
    lines.append("/aso skip — пропустить этот цикл")

    return "\n".join(lines)


def review(state: ASOState) -> Command:
    """Pause graph for human review.

    Sends plan to Telegram, then calls interrupt().
    The graph stays paused until resumed externally with one of:
    - {"action": "approve"}
    - {"action": "reject", "comment": "..."}
    - {"action": "skip"}
    """
    plan = state.get("optimization_plan")

    if not plan:
        log.warning("REVIEW: no plan to review, skipping to notify")
        return Command(goto="notify")

    # Send plan to Telegram
    message = _format_plan_for_telegram(state)
    _send_telegram(message)
    log.info("Plan sent to Telegram, waiting for human input...")

    # Pause the graph — this is where LangGraph magic happens
    human_input = interrupt({
        "type": "review_request",
        "cycle_id": state.get("cycle_id"),
        "plan_summary": f"{len(plan.get('changes', []))} changes, "
                       f"risk: {plan.get('risk_assessment', '?')}",
    })

    # Graph resumes here with human_input
    action = human_input.get("action", "skip")
    log.info("Human input received: action=%s", action)

    if action == "approve":
        return Command(goto="execute", update={"phase": "approved"})

    elif action == "reject":
        comment = human_input.get("comment", "")
        log.info("Plan rejected with feedback: %s", comment[:100])
        return Command(
            goto="plan",
            update={
                "phase": "revision",
                "human_feedback": comment,
                "optimization_plan": None,  # clear old plan
            },
        )

    else:  # skip
        log.info("Cycle skipped by user")
        return Command(
            goto="notify",
            update={
                "phase": "skipped",
                "selected_opportunity": None,
                "optimization_plan": None,
            },
        )
