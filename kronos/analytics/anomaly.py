"""Anomaly Detection — statistical + LLM-powered anomaly detection.

Two approaches:
1. Statistical: z-score based, 14-day baseline, weekend-aware
2. LLM: for complex cross-metric anomalies
"""

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from kronos.analytics import metric_store

log = logging.getLogger("kronos.analytics.anomaly")


@dataclass
class Anomaly:
    metric: str
    current: float
    expected: float
    z_score: float
    direction: str  # "spike" or "drop"
    severity: str   # "critical" (z > 3) or "warning" (z > 2)

    def format_alert(self) -> str:
        """Format anomaly for Telegram alert."""
        icon = "\U0001f6a8" if self.severity == "critical" else "\u26a0\ufe0f"
        arrow = "\u2191" if self.direction == "spike" else "\u2193"
        pct = abs(self.current - self.expected) / self.expected * 100 if self.expected else 0

        return (
            f"{icon} <b>Anomaly: {self.metric}</b>\n"
            f"{arrow} Текущее: {self.current:.2f}\n"
            f"Среднее (14д): {self.expected:.2f}\n"
            f"Отклонение: {pct:.0f}% (z={self.z_score:.1f})\n"
        )


# Metrics monitored for anomalies, with direction sensitivity.
# "both" = alert on spike AND drop; "up" = only spikes; "down" = only drops.
MONITORED_METRICS: dict[str, str] = {
    # Infra — alert on spikes
    "sentry.events_24h": "up",
    "grafana.error_rate_pct": "up",
    "grafana.firing_alerts": "up",
    "zabbix.active_problems": "up",
    # Product — alert on drops
    "posthog.dau": "down",
    "posthog.new_signups_24h": "down",
    # Revenue — alert on drops
    "revenuecat.mrr": "down",
    "revenuecat.active_subscribers": "down",
    # AI costs — alert on spikes
    "litellm.spend_24h_usd": "up",
}

# Weekend metrics that naturally drop (don't alert on Sat/Sun drops)
_WEEKEND_IGNORE_DROP = {"posthog.dau", "posthog.new_signups_24h"}


def check_anomaly(
    metric_name: str,
    current_value: float,
    direction: str = "both",
    min_history: int = 7,
) -> Anomaly | None:
    """Check if current value is anomalous using z-score.

    Args:
        metric_name: Metric identifier.
        current_value: Current observed value.
        direction: "up" (alert on spikes), "down" (drops), "both".
        min_history: Minimum data points needed for detection.

    Returns:
        Anomaly if detected, None otherwise.
    """
    history = metric_store.get_history(metric_name, days=14)

    if len(history) < min_history:
        return None  # Not enough data

    mean = statistics.mean(history)
    stdev = statistics.stdev(history)

    if stdev == 0:
        return None  # No variance — can't detect anomaly

    z_score = (current_value - mean) / stdev

    # Weekend check — ignore natural drops on Sat/Sun
    now = datetime.now(timezone.utc)
    is_weekend = now.weekday() >= 5
    if is_weekend and metric_name in _WEEKEND_IGNORE_DROP and z_score < 0:
        return None

    # Check direction
    if direction == "up" and z_score < 2:
        return None
    if direction == "down" and z_score > -2:
        return None
    if direction == "both" and abs(z_score) < 2:
        return None

    anomaly_direction = "spike" if z_score > 0 else "drop"
    severity = "critical" if abs(z_score) > 3 else "warning"

    return Anomaly(
        metric=metric_name,
        current=current_value,
        expected=round(mean, 2),
        z_score=round(abs(z_score), 2),
        direction=anomaly_direction,
        severity=severity,
    )


def check_all_anomalies(flat_metrics: dict[str, float]) -> list[Anomaly]:
    """Check all monitored metrics for anomalies.

    Args:
        flat_metrics: Flattened dict of metric_name → value.

    Returns:
        List of detected anomalies, sorted by z_score descending.
    """
    anomalies = []

    for metric_name, direction in MONITORED_METRICS.items():
        value = flat_metrics.get(metric_name)
        if value is None or not isinstance(value, (int, float)):
            continue

        anomaly = check_anomaly(metric_name, float(value), direction=direction)
        if anomaly:
            anomalies.append(anomaly)

    anomalies.sort(key=lambda a: a.z_score, reverse=True)
    return anomalies


def flatten_metrics(raw_metrics: dict[str, dict]) -> dict[str, float]:
    """Flatten nested source metrics into dot-notation keys.

    Example: {"posthog": {"dau": 342}} → {"posthog.dau": 342}
    """
    flat = {}
    for source, data in raw_metrics.items():
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if key == "error":
                continue
            if isinstance(value, (int, float)):
                flat[f"{source}.{key}"] = float(value)
    return flat
