"""Competitive Dashboard — auto-generated markdown for Outline.

Generates a comprehensive markdown dashboard with:
- App Store ratings table
- Competitive advantages
- Recent changes
- Historical trends
"""

import logging
from datetime import UTC, datetime, timedelta

from kronos.competitors.config import load_competitors
from kronos.competitors.store import CompetitorStore
from kronos.competitors.tracker import CompetitiveTracker

log = logging.getLogger("kronos.competitors.dashboard")

STATUS_EMOJI = {
    "strong": "\U0001f7e2",   # green
    "par": "\U0001f7e1",      # yellow
    "weak": "\U0001f7e0",     # orange
    "missing": "\U0001f534",  # red
}

TREND_ARROW = {
    "improving": "\u2191",
    "stable": "\u2192",
    "declining": "\u2193",
}


def generate_dashboard_markdown() -> str:
    """Generate full competitive dashboard as markdown."""
    store = CompetitorStore()
    tracker = CompetitiveTracker()
    competitors = load_competitors()
    now = datetime.now(UTC)

    sections = [
        "# Competitive Dashboard",
        f"*Updated: {now.strftime('%d %B %Y, %H:%M UTC')}*",
        "",
    ]

    # Section 1: App Store Ratings
    sections.append("## App Store Ratings (iOS)")
    sections.append(_ratings_table(store, competitors, "app_store_ios"))
    sections.append("")

    # Section 2: Play Store Ratings
    android_comps = [c for c in competitors if c.android_package]
    if android_comps:
        sections.append("## Play Store Ratings (Android)")
        sections.append(_ratings_table(store, android_comps, "app_store_android"))
        sections.append("")

    # Section 3: Competitive Advantages
    sections.append("## Competitive Advantages")
    sections.append(_advantages_table(tracker))
    sections.append("")

    # Section 4: Recent Changes (7 days)
    sections.append("## Recent Changes (7 days)")
    sections.append(_recent_changes(store))
    sections.append("")

    # Section 5: Historical Trends (30 days)
    sections.append("## Rating Trends (30 days)")
    sections.append(_rating_trends(store, competitors))

    return "\n".join(sections)


def _ratings_table(store: CompetitorStore, competitors: list, channel: str) -> str:
    """Generate ratings comparison table."""
    lines = ["| App | Rating | Reviews | Version | Trend |"]
    lines.append("|---|---|---|---|---|")

    for comp in competitors:
        snapshot = store.get_latest_snapshot(comp.id, channel)
        if not snapshot:
            lines.append(f"| {comp.name} | — | — | — | — |")
            continue

        rating = snapshot.get("rating", 0)
        count = snapshot.get("rating_count", 0)
        version = snapshot.get("version", "?")

        # Calculate trend from last 2 snapshots
        trend = _calc_rating_trend(store, comp.id, channel)

        lines.append(
            f"| {comp.name} | {rating:.1f} | "
            f"{_format_count(count)} | {version} | {trend} |"
        )

    return "\n".join(lines)


def _advantages_table(tracker: CompetitiveTracker) -> str:
    """Generate competitive advantages table."""
    rows = tracker.get_all()
    if not rows:
        return "No data yet."

    lines = ["| Area | Status | Leader | Trend | Notes |"]
    lines.append("|---|---|---|---|---|")

    for r in rows:
        status = r.get("our_status", "par")
        emoji = STATUS_EMOJI.get(status, "\u26aa")
        trend = TREND_ARROW.get(r.get("trend", "stable"), "\u2192")
        leader = r.get("competitor_leader", "") or "—"
        notes = (r.get("notes", "") or "—")[:40]
        area = r.get("feature_area", "").replace("_", " ").title()

        lines.append(f"| {area} | {emoji} {status} | {leader} | {trend} | {notes} |")

    return "\n".join(lines)


def _recent_changes(store: CompetitorStore) -> str:
    """Format recent changes list."""
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    changes = store._db.read(
        "SELECT * FROM competitor_changes WHERE detected_at >= ? "
        "ORDER BY detected_at DESC LIMIT 20",
        (week_ago,),
    )

    if not changes:
        return "No changes detected in the last 7 days."

    lines = []
    severity_emoji = {"critical": "\U0001f534", "important": "\U0001f7e1", "info": "\u2139\ufe0f"}
    for ch in [dict(r) for r in changes]:
        emoji = severity_emoji.get(ch["severity"], "\u2022")
        lines.append(f"- {emoji} {ch['summary']}")

    return "\n".join(lines)


def _rating_trends(store: CompetitorStore, competitors: list) -> str:
    """Generate text-based rating trend for each competitor."""
    lines = []

    for comp in competitors:
        snapshots = store._db.read(
            "SELECT json_extract(data, '$.rating') as rating, captured_at "
            "FROM competitor_snapshots "
            "WHERE competitor_id = ? AND channel = 'app_store_ios' "
            "AND captured_at >= datetime('now', '-30 days') "
            "ORDER BY captured_at",
            (comp.id,),
        )

        if len(snapshots) < 2:
            continue

        ratings = [dict(s) for s in snapshots]
        first = ratings[0].get("rating")
        last = ratings[-1].get("rating")

        if first is None or last is None:
            continue

        first, last = float(first), float(last)
        diff = last - first
        arrow = "\u2191" if diff > 0.05 else ("\u2193" if diff < -0.05 else "\u2192")

        lines.append(f"- **{comp.name}**: {first:.1f} {arrow} {last:.1f} ({diff:+.1f})")

    return "\n".join(lines) if lines else "Not enough data yet (need 2+ days of snapshots)."


def _calc_rating_trend(store: CompetitorStore, comp_id: str, channel: str) -> str:
    """Calculate simple trend arrow from last 2 snapshots."""
    rows = store._db.read(
        "SELECT json_extract(data, '$.rating') as rating "
        "FROM competitor_snapshots "
        "WHERE competitor_id = ? AND channel = ? "
        "ORDER BY captured_at DESC LIMIT 2",
        (comp_id, channel),
    )

    if len(rows) < 2:
        return "\u2192"

    curr = float(rows[0]["rating"] or 0)
    prev = float(rows[1]["rating"] or 0)

    if curr > prev + 0.05:
        return "\u2191"
    elif curr < prev - 0.05:
        return "\u2193"
    return "\u2192"


def _format_count(count: int) -> str:
    """Format large numbers: 12400 → 12.4K."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)
