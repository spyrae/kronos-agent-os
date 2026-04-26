"""Weekly deep competitive analysis report (Phase 3).

Runs every Sunday. Generates strategic analysis with trend detection,
competitive advantage updates, and Mem0 integration for long-term memory.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from kronos.competitors.config import load_competitors
from kronos.competitors.store import CompetitorStore
from kronos.competitors.tracker import CompetitiveTracker
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.competitors.weekly_report")


_PRODUCT_DESC = os.environ.get("PRODUCT_DESCRIPTION", "your product")
_PRODUCT_FEATURES = os.environ.get("PRODUCT_FEATURES", "")

WEEKLY_REPORT_PROMPT = f"""You are a strategic competitive intelligence analyst for {_PRODUCT_DESC}.

{f"Key features: {_PRODUCT_FEATURES}" if _PRODUCT_FEATURES else ""}

## Data for the week {date_range}:

### Changes detected this week:
{changes_summary}

### Current competitive position:
{tracker_summary}

### Past competitive knowledge (from memory):
{mem0_context}

## Generate a deep weekly competitive report:

1. **EXECUTIVE SUMMARY** (3-5 bullet points — most important observations)

2. **COMPETITIVE MOVES** (for each Tier 1 competitor with activity):
   - What they did
   - Why (strategic hypothesis)
   - Threat level: 🟢 Low / 🟡 Medium / 🔴 High

3. **MARKET TRENDS** (patterns visible across 2+ competitors)

4. **OUR POSITION**:
   - Where we lead
   - Where we lag
   - What changed this week

5. **ACTION ITEMS** (concrete, prioritized):
   - 🔴 Urgent (this week)
   - 🟡 Important (next sprint)
   - 🟢 Nice to have (backlog)

Write in Russian. Format for Telegram (use emoji, no markdown headers — use bold with ** instead).
Keep under 3500 chars."""

TRACKER_UPDATE_PROMPT = """Based on this week's competitive changes, update the competitive advantage tracker.

Changes this week:
{changes_summary}

Current tracker state:
{tracker_summary}

For each feature area that should be updated, return a JSON array:
[
  {{"feature_area": "area_id", "our_status": "strong|par|weak|missing", "competitor_leader": "CompanyName", "notes": "brief note", "trend": "improving|stable|declining"}}
]

Valid feature areas: ai_chat, itinerary, poi, visa, booking_import, offline, collaboration, maps, budget, social.
You can add new areas if a competitor launched something we don't track yet.
Only include areas that need updating. Return [] if no updates needed.
Return ONLY the JSON array, nothing else."""


async def generate_weekly_report() -> tuple[str, str]:
    """Generate weekly deep competitive analysis report.

    Returns:
        (report_text, executive_summary) — full report and short summary for Mem0.
    """
    store = CompetitorStore()
    tracker = CompetitiveTracker()
    competitors = load_competitors()

    # Gather week's changes
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    all_changes = store.get_undigested_changes()
    # Also get recent digested changes (full week)
    week_changes = store._db.read(
        "SELECT * FROM competitor_changes WHERE detected_at >= ? ORDER BY detected_at DESC",
        (week_ago.isoformat(),),
    )
    week_changes = [dict(r) for r in week_changes]

    if not week_changes:
        return "No competitive activity detected this week.", ""

    # Format changes for LLM
    changes_summary = _format_changes(week_changes)
    tracker_summary = tracker.format_summary()

    # Get Mem0 context
    mem0_context = await _get_mem0_context()

    # Date range
    date_range = f"{week_ago.strftime('%d %b')} — {now.strftime('%d %b %Y')}"

    # Generate report
    prompt = WEEKLY_REPORT_PROMPT.format(
        date_range=date_range,
        changes_summary=changes_summary,
        tracker_summary=tracker_summary,
        mem0_context=mem0_context,
    )

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([HumanMessage(content=prompt)])
    report = response.content if isinstance(response.content, str) else str(response.content)

    # Update competitive tracker via LLM
    await _update_tracker(tracker, changes_summary, tracker_summary)

    # Save to Mem0
    executive_summary = _extract_executive_summary(report)
    await _save_to_mem0(date_range, executive_summary)

    return report, executive_summary


async def _get_mem0_context() -> str:
    """Retrieve past competitive intelligence from Mem0."""
    try:
        from kronos.memory.store import search_memories

        memories = search_memories(
            "competitor trends competitive advantages market analysis",
            user_id="competitor_monitor",
            limit=5,
        )
        if memories:
            return "\n".join(f"- {m}" for m in memories)
    except Exception as e:
        log.warning("Mem0 search failed: %s", e)

    return "No previous competitive memory available."


async def _save_to_mem0(date_range: str, executive_summary: str) -> None:
    """Save weekly analysis summary to Mem0 for long-term accumulation."""
    if not executive_summary:
        return

    try:
        from kronos.memory.store import add_memories

        messages = [
            {"role": "user", "content": f"Weekly competitive analysis for {date_range}"},
            {"role": "assistant", "content": executive_summary},
        ]
        add_memories(messages, user_id="competitor_monitor")
        log.info("Saved weekly competitive summary to Mem0")
    except Exception as e:
        log.warning("Mem0 save failed: %s", e)


async def _update_tracker(
    tracker: CompetitiveTracker,
    changes_summary: str,
    tracker_summary: str,
) -> None:
    """Use LLM to update the competitive advantage tracker."""
    prompt = TRACKER_UPDATE_PROMPT.format(
        changes_summary=changes_summary,
        tracker_summary=tracker_summary,
    )

    model = get_model(ModelTier.LITE)
    response = model.invoke([HumanMessage(content=prompt)])
    raw = response.content if isinstance(response.content, str) else str(response.content)

    # Parse JSON from LLM response
    try:
        # Strip markdown code fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            clean = clean.rsplit("```", 1)[0]
        updates = json.loads(clean.strip())
        if isinstance(updates, list) and updates:
            tracker.bulk_update_from_llm(updates)
            log.info("Updated %d competitive advantages", len(updates))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse tracker updates: %s", e)


def _format_changes(changes: list[dict]) -> str:
    """Format changes list for LLM prompt."""
    if not changes:
        return "No changes this week."

    lines = []
    for ch in changes[:50]:  # cap at 50
        severity = ch.get("severity", "info")
        summary = ch.get("summary", "")
        channel = ch.get("channel", "")
        lines.append(f"[{severity}] ({channel}) {summary}")

    return "\n".join(lines)


def _extract_executive_summary(report: str) -> str:
    """Extract executive summary section from report for Mem0 storage."""
    # Look for the summary section (first 500 chars as fallback)
    lines = report.split("\n")
    summary_lines = []
    in_summary = False

    for line in lines:
        if "EXECUTIVE SUMMARY" in line.upper() or "SUMMARY" in line.upper():
            in_summary = True
            continue
        if in_summary:
            if line.strip().startswith(("2.", "**2", "COMPETITIVE", "COMPETITIVE MOVES")):
                break
            if line.strip():
                summary_lines.append(line.strip())

    if summary_lines:
        return "\n".join(summary_lines[:10])

    # Fallback: first 500 chars
    return report[:500]
