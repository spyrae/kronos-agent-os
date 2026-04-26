"""Competitor Digest — daily competitor monitoring cron job.

Runs only on Nexus agent. Fetches App Store / Play Store data for all
configured competitors, detects changes, synthesizes LLM digest, sends
to Telegram.
"""

import logging
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api

log = logging.getLogger("kronos.cron.competitor_digest")


async def run_competitor_digest() -> None:
    """Daily competitor monitoring — fetch, diff, digest, notify."""
    # Only runs on Nexus
    if settings.agent_name != "nexus":
        return

    from kronos.competitors.digest import CompetitorMonitor

    monitor = CompetitorMonitor()
    today = datetime.now(UTC).strftime("%d %B")

    try:
        digest = await monitor.run_daily_check()

        if digest is None:
            # First run (baseline) or no changes
            if monitor.last_changes_count == 0:
                log.info("No competitor changes detected")
                return

            # Baseline saved
            log.info(
                "Baseline saved for %d competitors — digest starts tomorrow",
                monitor.last_competitors_checked,
            )
            send_bot_api(
                f"<b>\U0001f4ca Competitor Monitor</b>\n\n"
                f"Baseline saved for {monitor.last_competitors_checked} competitors. "
                f"Daily digest starts tomorrow.",
                parse_mode="HTML",
                topic_id=TOPIC_DIGEST,
            )
            return

        # Send digest
        header = f"<b>\U0001f4ca Competitor Monitor \u2014 {today}</b>\n\n"
        send_bot_api(
            header + digest,
            parse_mode="HTML",
            topic_id=TOPIC_DIGEST,
        )
        log.info(
            "Competitor digest sent: %d changes from %d competitors",
            monitor.last_changes_count,
            monitor.last_competitors_checked,
        )

        # Update Outline dashboard after digest
        _update_dashboard()

    except Exception as e:
        log.error("Competitor digest failed: %s", e)
        send_bot_api(
            f"\u26a0\ufe0f Competitor digest failed: {str(e)[:200]}",
            topic_id=TOPIC_DIGEST,
        )


def _update_dashboard() -> None:
    """Regenerate and save Outline dashboard after digest."""
    try:
        from kronos.competitors.dashboard import generate_dashboard_markdown

        dashboard = generate_dashboard_markdown()
        # Save locally as fallback (Outline integration in future)
        from pathlib import Path

        from kronos.config import settings

        dashboard_path = Path(settings.db_dir) / "competitor_dashboard.md"
        dashboard_path.write_text(dashboard, encoding="utf-8")
        log.info("Dashboard updated: %d chars → %s", len(dashboard), dashboard_path)
    except Exception as e:
        log.warning("Dashboard update failed: %s", e)
