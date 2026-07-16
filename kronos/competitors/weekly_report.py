"""Weekly deep competitive analysis — sole competitor cron after daily/alerts removal.

Runs every Sunday. Performs the **full** intelligence cycle:

1. Fresh fetch across all channels (App Store, Play, website, blog RSS,
   Twitter, press, ProductHunt, jobs) for every tier-1 and tier-2 competitor.
2. Persists snapshots + diffs into competitor_monitor.db.
3. Aggregates **all** changes from the last 7 days (including the fresh ones).
4. Generates strategic analysis with WoW comparison via Mem0.
5. Updates the competitive advantage tracker.

Replaces the previous daily digest + 4-hour alerts setup. Trade-off:
slightly slower detection of in-week events, but one comprehensive report
instead of seven thin ones plus alert noise.
"""

import asyncio
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime, timedelta

from langchain_core.messages import HumanMessage

from kronos.competitors.config import load_competitors
from kronos.competitors.digest import CompetitorMonitor
from kronos.competitors.fetchers import _REQUEST_DELAY
from kronos.competitors.store import CompetitorStore
from kronos.competitors.tracker import CompetitiveTracker
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.competitors.weekly_report")


_PRODUCT_DESC = os.environ.get("PRODUCT_DESCRIPTION", "your product")
_PRODUCT_FEATURES = os.environ.get("PRODUCT_FEATURES", "")


WEEKLY_REPORT_PROMPT = """You are a strategic competitive intelligence analyst for {product_desc}.

{features_line}

## Window: {date_range}

### Activity volume this week
Total changes detected: {total_changes}
By channel: {channel_breakdown}
By severity: {severity_breakdown}
Competitors with activity: {active_competitors}/{total_competitors}

### Per-competitor activity (Tier 1 + Tier 2 with any signals)
{per_competitor}

### All raw changes (capped at 80 most relevant)
{changes_summary}

### Current competitive position (feature matrix)
{tracker_summary}

### Past weekly highlights (Mem0 long-term memory)
{mem0_context}

## Report structure (Russian, Telegram-friendly, ≤4500 chars)

Use plain text bold (**word**) where helpful — sender will convert to HTML.
NEVER use ###/## headings. Use emoji to group sections.

1. 📊 **EXECUTIVE SUMMARY** — 3-5 bullet points, most strategic observations.
   Be specific with numbers (e.g. "Wanderlog released 3 versions in 7 days").

2. 🎯 **COMPETITIVE MOVES** — for each Tier 1 competitor with signals:
   • What they did (concrete: version, blog post title, hiring sign, etc.)
   • Strategic hypothesis (why)
   • Threat level: 🟢 Low / 🟡 Medium / 🔴 High

3. 🌊 **MARKET TRENDS** — patterns visible across 2+ competitors
   (e.g. "Three competitors added AI itinerary editing this week").

4. 📍 **OUR POSITION**:
   • Where we lead (vs which competitor specifically)
   • Where we lag (and the most concrete gap)
   • What shifted this week

5. ⚡ **ACTION ITEMS** — concrete, prioritised:
   • 🔴 Urgent (this week)
   • 🟡 Important (next sprint)
   • 🟢 Nice-to-have (backlog)

If activity volume is low, say so honestly; do NOT invent moves to fill sections."""


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


async def _collect_fresh_signals() -> dict:
    """Run a full fetch+diff cycle for every competitor.

    Reuses CompetitorMonitor (the same pipeline the old daily digest used)
    but discards the LLM digest output — we just want fresh snapshots and
    persisted changes in the DB. The weekly LLM analysis runs separately
    below over the 7-day window.

    Returns a dict with stats for logging / observability.
    """
    monitor = CompetitorMonitor()
    competitors = monitor.competitors

    fresh_changes_total = 0
    failed = 0

    for comp in competitors:
        try:
            changes = await monitor._check_competitor(comp)
            fresh_changes_total += len(changes)
        except Exception as e:
            log.warning("Weekly fetch failed for %s: %s", comp.name, e)
            failed += 1
        # Be polite to upstream APIs (iTunes, Play, Brave/Exa)
        await asyncio.sleep(_REQUEST_DELAY)

    log.info(
        "Weekly fetch done: %d competitors, %d fresh changes, %d failed",
        len(competitors),
        fresh_changes_total,
        failed,
    )
    return {
        "competitors_checked": len(competitors),
        "fresh_changes": fresh_changes_total,
        "failed": failed,
    }


def _per_competitor_lines(week_changes: list[dict], competitors_by_id: dict) -> str:
    """Group changes by competitor, render compact summary.

    Skips competitors with zero activity to keep the prompt focused.
    """
    by_comp: dict[str, list[dict]] = {}
    for ch in week_changes:
        by_comp.setdefault(ch.get("competitor_id", "unknown"), []).append(ch)

    lines = []

    # Sort: tier-1 first by activity, then tier-2
    def _sort_key(cid):
        comp = competitors_by_id.get(cid)
        tier = comp.tier if comp else 9
        return (tier, -len(by_comp[cid]))

    for cid in sorted(by_comp.keys(), key=_sort_key):
        comp = competitors_by_id.get(cid)
        if not comp:
            continue
        items = by_comp[cid]
        channels = sorted({c.get("channel", "?") for c in items})
        lines.append(
            f"  • T{comp.tier} {comp.name} — {len(items)} changes "
            f"across {len(channels)} channels ({', '.join(channels[:4])})"
        )
        # Show the 3 most important per competitor
        sorted_items = sorted(
            items,
            key=lambda c: {"critical": 0, "important": 1, "info": 2}.get(c.get("severity", "info"), 3),
        )
        for it in sorted_items[:3]:
            lines.append(f"      [{it.get('severity', 'info')}] {it.get('summary', '')[:120]}")

    return "\n".join(lines) if lines else "(no per-competitor activity)"


async def generate_weekly_report() -> tuple[str, str]:
    """Generate the weekly competitive analysis report.

    Steps:
      1. Fresh signal collection (App Store, Play, web channels).
      2. Aggregate the last 7 days from the DB (includes the fresh data).
      3. Synthesise the report via LLM.
      4. Update tracker + Mem0.

    Returns:
        (report_text, executive_summary) — full report and short summary for Mem0.
    """
    fetch_stats = await _collect_fresh_signals()

    store = CompetitorStore()
    tracker = CompetitiveTracker()
    competitors = load_competitors()
    competitors_by_id = {c.id: c for c in competitors}

    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)

    # All changes from the last 7 days (regardless of digested flag)
    rows = store._db.read(
        "SELECT * FROM competitor_changes WHERE detected_at >= ? ORDER BY detected_at DESC",
        (week_ago.isoformat(),),
    )
    week_changes = [dict(r) for r in rows]

    if not week_changes:
        return "No competitive activity detected this week.", ""

    # Aggregations for the prompt header
    channel_counter = Counter(c.get("channel", "?") for c in week_changes)
    severity_counter = Counter(c.get("severity", "info") for c in week_changes)
    active_competitor_ids = {c.get("competitor_id") for c in week_changes}

    channel_breakdown = ", ".join(f"{ch}={cnt}" for ch, cnt in channel_counter.most_common(8))
    severity_breakdown = ", ".join(f"{sev}={cnt}" for sev, cnt in severity_counter.most_common())

    per_competitor = _per_competitor_lines(week_changes, competitors_by_id)
    changes_summary = _format_changes(week_changes)
    tracker_summary = tracker.format_summary()
    mem0_context = await _get_mem0_context()

    date_range = f"{week_ago.strftime('%d %b')} — {now.strftime('%d %b %Y')}"

    prompt = WEEKLY_REPORT_PROMPT.format(
        product_desc=_PRODUCT_DESC,
        features_line=f"Key features: {_PRODUCT_FEATURES}" if _PRODUCT_FEATURES else "",
        date_range=date_range,
        total_changes=len(week_changes),
        channel_breakdown=channel_breakdown or "—",
        severity_breakdown=severity_breakdown or "—",
        active_competitors=len(active_competitor_ids),
        total_competitors=len(competitors),
        per_competitor=per_competitor,
        changes_summary=changes_summary,
        tracker_summary=tracker_summary,
        mem0_context=mem0_context,
    )

    model = get_model(ModelTier.STANDARD)
    response = model.invoke([HumanMessage(content=prompt)])
    report = response.content if isinstance(response.content, str) else str(response.content)

    # Append a small operational footer so the user can see fetch stats
    report = (
        report.rstrip()
        + f"\n\n— scan: {fetch_stats['competitors_checked']} competitors, "
        + f"{fetch_stats['fresh_changes']} new changes "
        + f"({fetch_stats['failed']} failed)"
    )

    await _update_tracker(tracker, changes_summary, tracker_summary)

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

    try:
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
    """Format changes list for LLM prompt — sorted by severity then date."""
    if not changes:
        return "No changes this week."

    severity_order = {"critical": 0, "important": 1, "info": 2}
    sorted_changes = sorted(
        changes,
        key=lambda c: (
            severity_order.get(c.get("severity", "info"), 3),
            c.get("detected_at", ""),
        ),
    )

    lines = []
    for ch in sorted_changes[:80]:  # cap for token budget
        severity = ch.get("severity", "info")
        summary = ch.get("summary", "")
        channel = ch.get("channel", "")
        detected = (ch.get("detected_at") or "")[:10]
        lines.append(f"[{detected} {severity:9s} {channel}] {summary}")

    if len(sorted_changes) > 80:
        lines.append(f"... and {len(sorted_changes) - 80} more")

    return "\n".join(lines)


def _extract_executive_summary(report: str) -> str:
    """Extract executive summary section from report for Mem0 storage."""
    lines = report.split("\n")
    summary_lines = []
    in_summary = False

    for line in lines:
        upper = line.upper()
        if "EXECUTIVE SUMMARY" in upper:
            in_summary = True
            continue
        if in_summary:
            # Stop at the next numbered section
            if line.strip().startswith(("2.", "**2", "COMPETITIVE MOVES")):
                break
            if line.strip():
                summary_lines.append(line.strip())

    if summary_lines:
        return "\n".join(summary_lines[:10])

    return report[:500]
