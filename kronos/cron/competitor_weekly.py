"""Weekly Competitor Analysis — deep strategic report.

Runs every Sunday at 10:00 UTC. Only on Nexus agent.
Generates deep analysis report → Telegram + Mem0.
"""

import logging

from kronos.config import settings
from kronos.cron.notify import send_bot_api, TOPIC_DIGEST

log = logging.getLogger("kronos.cron.competitor_weekly")


async def run_competitor_weekly() -> None:
    """Weekly deep competitive analysis report."""
    if settings.agent_name != "nexus":
        return

    from kronos.competitors.weekly_report import generate_weekly_report

    try:
        report, executive_summary = await generate_weekly_report()

        if not report or report == "No competitive activity detected this week.":
            log.info("No competitive activity this week, skipping report")
            return

        # Send to Telegram (may be split into multiple messages by send_bot_api)
        header = "<b>\U0001f4ca Weekly Competitive Analysis</b>\n\n"
        send_bot_api(
            header + report,
            parse_mode="HTML",
            topic_id=TOPIC_DIGEST,
        )

        log.info("Weekly competitive report sent (%d chars)", len(report))

    except Exception as e:
        log.error("Weekly competitor report failed: %s", e)
        send_bot_api(
            f"\u26a0\ufe0f Weekly competitor report failed: {str(e)[:200]}",
            topic_id=TOPIC_DIGEST,
        )
