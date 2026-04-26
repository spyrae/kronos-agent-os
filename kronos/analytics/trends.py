"""Trend Analysis — 4-week trend detection for weekly reports.

Analyzes metric history to identify growing, declining, and stagnating trends.
Used by weekly_report.py to add trend context.
"""

import logging
from dataclasses import dataclass

from kronos.analytics import metric_store

log = logging.getLogger("kronos.analytics.trends")

# Key metrics to track trends for
TREND_METRICS = [
    "posthog.dau",
    "posthog.new_signups_24h",
    "revenuecat.mrr",
    "revenuecat.active_subscribers",
    "litellm.spend_24h_usd",
    "sentry.events_24h",
    "supabase.total_users",
    "app_store.ios_rating",
]


@dataclass
class Trend:
    metric: str
    direction: str  # "↑↑", "↑", "→", "↓", "↓↓"
    current: float
    previous: float
    change_pct: float

    def format_line(self) -> str:
        metric_label = self.metric.split(".")[-1].replace("_", " ")
        return f"  {self.direction} {metric_label}: {self.current:.1f} ({self.change_pct:+.1f}%)"


def _weekly_average(metric_name: str, days_offset: int, window: int = 7) -> float | None:
    """Get average value for a metric in a specific week window."""
    # Get all history, slice by offset
    history = metric_store.get_history(metric_name, days=days_offset + window)
    if not history:
        return None

    # Take the earlier portion (offset..offset+window)
    if days_offset > 0 and len(history) > window:
        relevant = history[:-days_offset] if days_offset < len(history) else history
        relevant = relevant[-window:]
    else:
        relevant = history[-window:]

    if not relevant:
        return None

    import statistics
    return statistics.mean(relevant)


def analyze_trends() -> list[Trend]:
    """Analyze 4-week trends for key metrics.

    Compares this week's average to previous week's average.
    Returns list of Trend objects sorted by absolute change.
    """
    trends = []

    for metric_name in TREND_METRICS:
        current = _weekly_average(metric_name, days_offset=0, window=7)
        previous = _weekly_average(metric_name, days_offset=7, window=7)

        if current is None or previous is None:
            continue

        if previous == 0:
            change_pct = 100.0 if current > 0 else 0.0
        else:
            change_pct = (current - previous) / previous * 100

        # Determine direction
        if change_pct > 15:
            direction = "\u2191\u2191"  # ↑↑ fast growth
        elif change_pct > 3:
            direction = "\u2191"  # ↑ growth
        elif change_pct < -15:
            direction = "\u2193\u2193"  # ↓↓ fast decline
        elif change_pct < -3:
            direction = "\u2193"  # ↓ decline
        else:
            direction = "\u2192"  # → stable

        trends.append(Trend(
            metric=metric_name,
            direction=direction,
            current=round(current, 2),
            previous=round(previous, 2),
            change_pct=round(change_pct, 1),
        ))

    trends.sort(key=lambda t: abs(t.change_pct), reverse=True)
    return trends


def format_trends_summary(trends: list[Trend]) -> str:
    """Format trends for inclusion in weekly report prompt."""
    if not trends:
        return "Недостаточно исторических данных для анализа трендов."

    lines = ["4-week trend analysis:"]
    for t in trends:
        lines.append(t.format_line())
    return "\n".join(lines)
