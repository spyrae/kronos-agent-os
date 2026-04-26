"""Competitor Alerts — real-time critical change monitoring.

Runs every 4 hours on Nexus. Checks tier-1 competitors for critical
changes (pricing, rating drops) and sends immediate Telegram alerts.
"""

import logging

from kronos.config import settings
from kronos.cron.notify import send_bot_api, send_ntfy, TOPIC_GENERAL

log = logging.getLogger("kronos.cron.competitor_alerts")


async def run_competitor_alerts() -> None:
    """Check for critical competitor changes and alert immediately."""
    if settings.agent_name != "nexus":
        return

    from kronos.competitors.alerts import check_critical_alerts, format_alert

    try:
        critical = await check_critical_alerts()

        if not critical:
            return

        for change in critical:
            msg = format_alert(change)

            # Telegram message
            send_bot_api(msg, parse_mode="HTML", topic_id=TOPIC_GENERAL)

            # Push notification for critical alerts
            send_ntfy(
                f"{change.competitor_name}: {change.summary}",
                title="Competitor Alert",
                priority="high",
                tags="warning,chart_with_upwards_trend",
            )

        log.info("Sent %d critical competitor alerts", len(critical))

    except Exception as e:
        log.error("Competitor alerts check failed: %s", e)
