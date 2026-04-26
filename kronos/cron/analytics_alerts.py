"""Analytics Anomaly Alerts — periodic check for critical metric deviations.

Runs every 2 hours on Nexus. Checks critical infra/product metrics
against 14-day statistical baselines. Max 3 alerts per day to prevent
alert fatigue.
"""

import logging
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api, send_ntfy

log = logging.getLogger("kronos.cron.analytics_alerts")

MAX_DAILY_ALERTS = 3

# Track daily alert count (reset at midnight UTC via _check_reset)
_alert_state = {"count": 0, "date": ""}


def _check_reset() -> None:
    """Reset daily counter if date changed."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if _alert_state["date"] != today:
        _alert_state["count"] = 0
        _alert_state["date"] = today


async def run_analytics_alerts() -> None:
    """Check critical metrics for anomalies and send alerts."""
    if settings.agent_name != "nexus":
        return

    _check_reset()

    if _alert_state["count"] >= MAX_DAILY_ALERTS:
        log.debug("Daily alert cap reached (%d), skipping", MAX_DAILY_ALERTS)
        return

    from kronos.analytics.anomaly import check_all_anomalies, flatten_metrics
    from kronos.analytics.sources import grafana, sentry, zabbix

    # Collect only critical sources (fast check, not all 10)
    critical_sources = {
        "zabbix": zabbix.collect(),
        "grafana": grafana.collect(),
        "sentry": sentry.collect(),
    }

    flat = flatten_metrics(critical_sources)
    if not flat:
        return

    anomalies = check_all_anomalies(flat)
    if not anomalies:
        return

    # Send alerts for remaining daily budget
    remaining = MAX_DAILY_ALERTS - _alert_state["count"]
    alerts_to_send = anomalies[:remaining]

    if len(anomalies) > remaining:
        # Bundle remaining as summary
        summary = (
            f"\u26a0\ufe0f <b>+{len(anomalies) - remaining} аномалий</b> "
            f"за последние 2 часа (лимит alerts: {MAX_DAILY_ALERTS}/день)"
        )
        bundle = True
    else:
        summary = ""
        bundle = False

    for anomaly in alerts_to_send:
        msg = anomaly.format_alert()
        send_bot_api(msg, parse_mode="HTML", topic_id=TOPIC_DIGEST)
        _alert_state["count"] += 1

        # NTFY push for critical anomalies
        if anomaly.severity == "critical":
            send_ntfy(
                f"ANOMALY: {anomaly.metric} {anomaly.direction} "
                f"(z={anomaly.z_score})",
                priority=4,
            )

        log.info(
            "Anomaly alert sent: %s %s (z=%.1f, %s)",
            anomaly.metric, anomaly.direction, anomaly.z_score, anomaly.severity,
        )

    if bundle and summary:
        send_bot_api(summary, parse_mode="HTML", topic_id=TOPIC_DIGEST)

    log.info("Anomaly check: %d detected, %d alerted", len(anomalies), len(alerts_to_send))
