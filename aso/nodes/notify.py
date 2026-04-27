"""NOTIFY node — send results to Telegram.

Handles notifications for all pipeline phases:
- Monitor+Analyze summary (no action needed)
- Execution confirmation
- Evaluation results
- Errors
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from ..state import ASOState

log = logging.getLogger("aso.nodes.notify")

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
        log.info("Telegram message sent (%d chars)", len(text))
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)


def notify(state: ASOState) -> dict:
    """Send appropriate notification based on current phase."""
    phase = state.get("phase", "unknown")
    cycle_id = state.get("cycle_id", "?")

    if phase == "evaluate":
        message = _format_evaluation(state)
    elif phase in ("execute", "approved"):
        message = _format_execution(state)
    elif phase == "skipped":
        message = f"📱 ASO #{cycle_id} — Цикл пропущен по запросу."
    else:
        message = _format_monitor_report(state)

    _send_telegram(message)
    return {"phase": "notified"}


# --- Formatters ---

def _format_monitor_report(state: ASOState) -> str:
    """Format monitor + analysis results."""
    cycle_id = state.get("cycle_id", "?")
    error = state.get("error")
    opportunities = state.get("opportunities", [])
    selected = state.get("selected_opportunity")

    parts = [f"📱 ASO Report — #{cycle_id}"]

    if error:
        parts.append(f"\n⚠️ {error}")

    # Monitor summary
    parts.append("")
    parts.append(_format_monitor_data(state))

    # Opportunities
    parts.append("")
    if not opportunities:
        parts.append("Opportunities: не найдены")
    else:
        parts.append(f"Opportunities: {len(opportunities)}")
        for opp in opportunities:
            icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(opp.get("priority", ""), "⚪")
            parts.append(f"  {icon} [{opp.get('type')}] {opp.get('description', '')[:120]}")

    if selected:
        parts.append(f"\n🎯 [{selected.get('type')}] {selected.get('description', '')[:150]}")
    elif opportunities:
        parts.append("\n✅ Нет actionable opportunities (все low priority)")
    else:
        parts.append("\n✅ Всё в порядке, действий не требуется")

    return "\n".join(parts)


def _format_monitor_data(state: ASOState) -> str:
    """Compact monitor data summary."""
    parts = []

    # Version state
    metadata = state.get("metadata_ios", {})
    if metadata:
        sample = next(iter(metadata.values()), {})
        version_state = sample.get("_version_state", "?")
        parts.append(f"State: {version_state} ({len(metadata)} locales)")

    # Ratings
    reviews = state.get("reviews_summary", {})
    if reviews.get("avg_rating"):
        parts.append(f"Rating: {reviews['avg_rating']:.1f} ({reviews.get('total_ratings', '?')} ratings)")
    elif reviews.get("_note"):
        parts.append(f"Rating: {reviews['_note']}")

    # Keywords
    rankings = state.get("keyword_rankings", {})
    if rankings:
        found = sum(1 for r in rankings.values() if r.get("found"))
        parts.append(f"Keywords: {found}/{len(rankings)} in top-50")
        ranked = [
            (r["keyword"], r["position"])
            for r in rankings.values()
            if r.get("position")
        ]
        ranked.sort(key=lambda x: x[1])
        for kw, pos in ranked[:5]:
            parts.append(f"  #{pos} \"{kw}\"")

    # Competitors
    competitors = state.get("competitor_data", [])
    if competitors:
        comp_str = ", ".join(
            f"{c.get('competitor_name', '?')} ({c.get('average_rating', '?')}⭐)"
            for c in competitors[:3]
        )
        parts.append(f"Competitors: {comp_str}")

    return "\n".join(parts)


def _format_execution(state: ASOState) -> str:
    """Format execution results."""
    cycle_id = state.get("cycle_id", "?")
    changes = state.get("changes_applied", {})
    plan = state.get("optimization_plan", {})

    applied = changes.get("applied", [])
    errors = changes.get("errors", [])

    parts = [f"✅ ASO #{cycle_id} — Изменения применены"]
    parts.append("")

    for ch in applied:
        parts.append(f"  {ch.get('locale')}.{ch.get('field')}:")
        parts.append(f"    \"{ch.get('old_value', '')}\"")
        parts.append(f"    → \"{ch.get('new_value', '')}\"")

    if errors:
        parts.append(f"\n⚠️ Ошибки ({len(errors)}):")
        for err in errors:
            parts.append(f"  {err.get('error', '?')}")

    days = plan.get("measurement_period_days", 14)
    parts.append(f"\nИзмерение через {days} дней.")

    return "\n".join(parts)


def _format_evaluation(state: ASOState) -> str:
    """Format evaluation results."""
    cycle_id = state.get("cycle_id", "?")
    evaluation = state.get("evaluation", {})
    verdict = evaluation.get("verdict", "unknown")

    verdict_icons = {
        "success": "🟢",
        "partial_success": "🟡",
        "neutral": "⚪",
        "failure": "🔴",
        "insufficient_data": "❓",
        "error": "⚠️",
    }
    icon = verdict_icons.get(verdict, "❓")

    parts = [f"📊 ASO #{cycle_id} — Результаты"]
    parts.append(f"\n{icon} Verdict: {verdict}")

    # Metrics
    metrics = evaluation.get("metrics", {})
    if metrics:
        parts.append("\nМетрики:")
        for key, val in metrics.items():
            if isinstance(val, dict):
                before = val.get("before", "?")
                after = val.get("after", "?")
                delta = val.get("delta_pct") or val.get("delta", "?")
                parts.append(f"  {key}: {before} → {after} ({delta})")
            else:
                parts.append(f"  {key}: {val}")

    # Learnings
    learnings = evaluation.get("learnings", [])
    if learnings:
        parts.append("\nLearnings:")
        for l in learnings:
            parts.append(f"  • {l}")

    # Next steps
    recs = evaluation.get("next_recommendations", [])
    if recs:
        parts.append("\nNext:")
        for r in recs:
            parts.append(f"  → {r}")

    return "\n".join(parts)
