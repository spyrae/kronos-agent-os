"""Weekly SEO/GEO check + daily GSC refresh.

- ``run_seo_geo_weekly``: Sunday 03:00 UTC (06:00 MSK). Full check
  across all engines, positions for Tier A+B keywords, GEO citations
  for all questions, GSC pull. Sends formatted Telegram report.
- ``run_seo_geo_gsc_daily``: every day 02:00 UTC. Refresh GSC top
  queries so the daily pulse has fresh numbers.
"""

from __future__ import annotations

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_JB_SYSTEM, send_bot_api

log = logging.getLogger("kronos.cron.seo_geo")


async def run_seo_geo_weekly() -> None:
    """Full weekly check — Nexus only."""
    if settings.agent_name != "nexus":
        return
    from kronos.seo_geo.reporter import format_weekly_report
    from kronos.seo_geo.runner import run_full_check
    try:
        counts = run_full_check(tiers=("A", "B"))
        log.info("seo_geo weekly counts: %s", counts)
        report = format_weekly_report()
        if report:
            send_bot_api(report, parse_mode="HTML", topic_id=TOPIC_JB_SYSTEM)
    except Exception as e:
        log.error("seo_geo weekly failed: %s", e)
        send_bot_api(
            f"⚠️ Еженедельная SEO/GEO-проверка не собралась: {str(e)[:200]}",
            topic_id=TOPIC_JB_SYSTEM,
        )


async def run_seo_geo_gsc_daily() -> None:
    """Daily GSC refresh — Nexus only."""
    if settings.agent_name != "nexus":
        return
    from kronos.seo_geo.runner import run_gsc_only
    try:
        counts = run_gsc_only()
        log.info("seo_geo gsc counts: %s", counts)
    except Exception as e:
        log.warning("seo_geo gsc daily failed: %s", e)
