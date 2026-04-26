"""Real-time critical alerts for competitor changes.

Runs every 4 hours (more frequent than daily digest).
Only checks tier-1 competitors for critical signals:
- Pricing changes
- Rating drops below thresholds
- ProductHunt launches
"""

import logging
from datetime import UTC, datetime

from kronos.competitors.config import load_competitors
from kronos.competitors.diff import diff_snapshots
from kronos.competitors.fetchers import fetch_all_for_competitor
from kronos.competitors.models import Change, ChangeType, Severity
from kronos.competitors.store import CompetitorStore

log = logging.getLogger("kronos.competitors.alerts")

# Max alerts per day to avoid alert fatigue
MAX_DAILY_ALERTS = 3


async def check_critical_alerts() -> list[Change]:
    """Quick check for critical changes on tier-1 competitors.

    Returns list of critical changes that should trigger immediate alerts.
    """
    store = CompetitorStore()
    competitors = load_competitors()
    tier1 = [c for c in competitors if c.tier == 1]

    if not tier1:
        return []

    # Check daily alert count
    today_count = _get_today_alert_count(store)
    if today_count >= MAX_DAILY_ALERTS:
        log.info("Daily alert cap reached (%d), skipping", today_count)
        return []

    critical_changes: list[Change] = []

    for comp in tier1:
        try:
            snapshots = await fetch_all_for_competitor(comp.ios_id, comp.android_package)

            for channel, snapshot in snapshots.items():
                curr = snapshot.to_dict()
                prev = store.get_latest_snapshot(comp.id, channel)

                if prev is None:
                    continue  # No baseline yet

                changes = diff_snapshots(comp.id, comp.name, channel, prev, curr)

                for ch in changes:
                    if ch.severity == Severity.CRITICAL:
                        critical_changes.append(ch)

                # Save snapshot (keeps data fresh between daily runs)
                store.save_snapshot(comp.id, channel, curr)

                # Persist the change
                for ch in changes:
                    store.save_change(
                        competitor_id=ch.competitor_id,
                        channel=ch.channel,
                        change_type=ch.change_type.value,
                        severity=ch.severity.value,
                        summary=ch.summary,
                        details=ch.details,
                    )

        except Exception as e:
            log.warning("Alert check failed for %s: %s", comp.name, e)

    # Cap alerts
    remaining = MAX_DAILY_ALERTS - today_count
    if len(critical_changes) > remaining:
        critical_changes = critical_changes[:remaining]

    # Record alert count
    if critical_changes:
        _record_alerts(store, len(critical_changes))

    return critical_changes


def format_alert(change: Change) -> str:
    """Format a critical change as a Telegram alert message."""
    lines = [
        f"\U0001f6a8 <b>ALERT: {change.competitor_name}</b>",
        "",
        change.summary,
    ]

    if change.details:
        if change.change_type == ChangeType.PRICING_CHANGE:
            lines.append(
                f"\nOld: ${change.details.get('old_price', '?')} "
                f"\u2192 New: ${change.details.get('new_price', '?')}"
            )
        elif change.change_type == ChangeType.VERSION_UPDATE:
            notes = change.details.get("release_notes", "")
            if notes and notes != "No notes":
                lines.append(f"\nRelease notes: {notes[:300]}")

    return "\n".join(lines)


def _get_today_alert_count(store: CompetitorStore) -> int:
    """Get number of alerts sent today."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    row = store._db.read_one(
        "SELECT COUNT(*) as cnt FROM competitor_changes "
        "WHERE severity = 'critical' AND detected_at >= ?",
        (today,),
    )
    return row["cnt"] if row else 0


def _record_alerts(store: CompetitorStore, count: int) -> None:
    """Record that alerts were sent (for daily cap tracking)."""
    log.info("Sending %d critical alerts", count)
