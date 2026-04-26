"""Weekly Business Report — full company health cron job.

Runs Monday 09:00 UTC on Nexus agent. Aggregates all sources for
7-day view, sends executive summary to Telegram.
"""

import logging
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api

log = logging.getLogger("kronos.cron.analytics_weekly")


async def run_analytics_weekly() -> None:
    """Weekly business report — collect all metrics, synthesize, notify."""
    if settings.agent_name != "nexus":
        return

    from kronos.analytics.weekly_report import generate_weekly_report

    today = datetime.now(UTC)
    week_label = today.strftime("W%V %Y")

    try:
        report, metrics = await generate_weekly_report()

        if not report or len(report) < 100:
            log.warning("Empty weekly report generated, skipping")
            return

        # Count working sources
        ok = sum(1 for m in metrics.values() if "error" not in m)
        failed = sum(1 for m in metrics.values() if "error" in m)

        header = f"<b>\U0001f4c8 Weekly Business Report \u2014 {week_label}</b>\n\n"

        # Split if too long for Telegram (4096 char limit)
        full_text = header + report
        if len(full_text) <= 4096:
            send_bot_api(full_text, parse_mode="HTML", topic_id=TOPIC_DIGEST)
        else:
            # Send in 2 parts
            mid = len(report) // 2
            # Find a good split point (newline)
            split_at = report.rfind("\n", 0, mid + 200)
            if split_at < mid - 200:
                split_at = mid

            send_bot_api(header + report[:split_at], parse_mode="HTML", topic_id=TOPIC_DIGEST)
            send_bot_api(report[split_at:], parse_mode="HTML", topic_id=TOPIC_DIGEST)

        log.info("Weekly business report sent: %d sources OK, %d failed", ok, failed)

    except Exception as e:
        log.error("Weekly business report failed: %s", e)
        send_bot_api(
            f"\u26a0\ufe0f Weekly report failed: {str(e)[:200]}",
            topic_id=TOPIC_DIGEST,
        )
