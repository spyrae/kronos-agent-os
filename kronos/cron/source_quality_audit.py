"""Biweekly Signal Intelligence source quality audit."""

import logging

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST_NEWS, send_bot_api

log = logging.getLogger("kronos.cron.source_quality_audit")
SOURCE_QUALITY_AUDIT_TELEGRAM_ENABLED = False


async def run_source_quality_audit() -> None:
    """Persist source keep/drop recommendations every ~14 days."""
    if settings.agent_name != "kronos":
        return

    from kronos.signals.quality import build_source_quality_audit, has_recent_source_quality_audit
    from kronos.signals.store import SignalStore

    store = SignalStore()
    if has_recent_source_quality_audit(store=store):
        log.info("Skipping source quality audit: recent audit already exists")
        return

    audit = build_source_quality_audit(store=store, dry_run=False)
    if SOURCE_QUALITY_AUDIT_TELEGRAM_ENABLED:
        send_bot_api(audit.body, parse_mode="HTML", topic_id=TOPIC_DIGEST_NEWS)
    log.info(
        "Source quality audit generated: %d recommendations, digest_id=%s, telegram=%s",
        len(audit.recommendations),
        audit.saved_digest_id,
        SOURCE_QUALITY_AUDIT_TELEGRAM_ENABLED,
    )
