"""Weekly Competitor Analysis — deep strategic report.

Paused since 2026-07-03. When enabled, runs weekly on Nexus and sends the
deep analysis report to Telegram + Mem0.
"""

import logging

from kronos.config import settings

log = logging.getLogger("kronos.cron.competitor_weekly")
COMPETITOR_WEEKLY_ENABLED = False


async def run_competitor_weekly() -> None:
    """Weekly deep competitive analysis report."""
    if not COMPETITOR_WEEKLY_ENABLED:
        log.info("Weekly competitor report disabled; skipping collection/analysis/publication")
        return
    if settings.agent_name != "nexus":
        return

    from kronos.competitors.weekly_report import generate_weekly_report
    from kronos.cron.notify import TOPIC_JB_COMPETITORS, send_bot_api

    try:
        report, executive_summary = await generate_weekly_report()

        if not report or report == "No competitive activity detected this week.":
            log.info("No competitive activity this week, skipping report")
            return

        # Send to Telegram (may be split into multiple messages by send_bot_api)
        header = "<b>\U0001f4ca Еженедельный анализ конкурентов</b>\n\n"
        send_bot_api(
            header + report,
            parse_mode="HTML",
            topic_id=TOPIC_JB_COMPETITORS,
        )

        log.info("Weekly competitive report sent (%d chars)", len(report))

    except Exception as e:
        log.error("Weekly competitor report failed: %s", e)
        send_bot_api(
            f"\u26a0\ufe0f Еженедельный анализ конкурентов не собрался: {str(e)[:200]}",
            topic_id=TOPIC_JB_COMPETITORS,
        )
