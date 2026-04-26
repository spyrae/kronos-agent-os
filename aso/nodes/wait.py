"""WAIT node — scheduled pause for measurement period.

Uses interrupt() to pause the graph for N days.
An external cron job or manual trigger resumes the graph.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langgraph.types import interrupt

from ..state import ASOState

log = logging.getLogger("aso.nodes.wait")


def wait(state: ASOState) -> dict:
    """Pause graph for measurement period.

    The graph will be resumed by:
    1. External cron: checks checkpoint DB, resumes if wait period elapsed
    2. Manual: /aso resume command
    """
    plan = state.get("optimization_plan", {})
    days = plan.get("measurement_period_days", 14)
    changes = state.get("changes_applied", {})

    now = datetime.now(timezone.utc)
    resume_at = now + timedelta(days=days)

    log.info(
        "=== WAIT: pausing for %d days (resume at %s) ===",
        days,
        resume_at.isoformat(),
    )

    # Notify about the wait
    applied_count = changes.get("success_count", 0)
    log.info(
        "Changes applied: %d. Waiting %d days to measure impact.",
        applied_count, days,
    )

    # interrupt() pauses the graph — state is persisted in SQLite
    interrupt({
        "type": "scheduled_wait",
        "cycle_id": state.get("cycle_id"),
        "resume_after_days": days,
        "resume_at": resume_at.isoformat(),
        "changes_applied_count": applied_count,
        "message": f"Ожидание {days} дней для сбора статистики. "
                  f"Возобновление: {resume_at.strftime('%Y-%m-%d')}.",
    })

    # Execution continues here after resume
    log.info("=== WAIT: resumed, proceeding to measurement ===")

    return {
        "phase": "measuring",
        "wait_started": now.isoformat(),
        "wait_ended": datetime.now(timezone.utc).isoformat(),
    }
