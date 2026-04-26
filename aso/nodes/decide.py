"""DECIDE node — determine if action is needed.

Rule-based, no LLM. Checks if any high/medium priority
opportunities exist and selects the best one.
"""

from __future__ import annotations

import logging

from ..state import ASOState, Opportunity

log = logging.getLogger("aso.nodes.decide")

# Scoring weights for opportunity ranking
PRIORITY_SCORE = {"high": 3, "medium": 2, "low": 1}
EFFORT_SCORE = {"low": 3, "medium": 2, "high": 1}  # inverted: low effort = better


def _score_opportunity(opp: Opportunity) -> float:
    """Score an opportunity by impact/effort ratio."""
    priority = PRIORITY_SCORE.get(opp.get("priority", "medium"), 2)
    effort = EFFORT_SCORE.get(opp.get("effort", "medium"), 2)
    return priority * effort


def decide(state: ASOState) -> dict:
    """Select best opportunity or decide to skip this cycle.

    Returns:
        - selected_opportunity if actionable
        - phase = "done" if nothing to do
    """
    opportunities = state.get("opportunities", [])

    if not opportunities:
        log.info("DECIDE: no opportunities found, cycle complete")
        return {
            "phase": "done",
            "selected_opportunity": None,
        }

    # Filter: only high and medium priority
    actionable = [
        opp for opp in opportunities
        if opp.get("priority") in ("high", "medium")
    ]

    if not actionable:
        log.info("DECIDE: %d opportunities, but none high/medium priority", len(opportunities))
        return {
            "phase": "done",
            "selected_opportunity": None,
        }

    # Rank by score and pick the best
    actionable.sort(key=_score_opportunity, reverse=True)
    selected = actionable[0]

    log.info(
        "DECIDE: selected '%s' (%s priority, %s effort) from %d candidates",
        selected.get("type"),
        selected.get("priority"),
        selected.get("effort"),
        len(actionable),
    )

    return {
        "phase": "decide",
        "selected_opportunity": selected,
    }
