"""Daily Pulse — full health + product digest cron job.

Runs at 08:30 UTC on Nexus agent. Collects all metrics (infra, product,
revenue, AI costs), sends summary to Telegram, and records metrics to
metric_store for anomaly detection baselines.
"""

import logging
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api

log = logging.getLogger("kronos.cron.analytics_pulse")


async def run_analytics_pulse() -> None:
    """Daily health pulse — collect metrics, synthesize, store, notify."""
    if settings.agent_name != "nexus":
        return

    from kronos.analytics import metric_store
    from kronos.analytics.anomaly import flatten_metrics
    from kronos.analytics.pulse import generate_daily_pulse

    today = datetime.now(UTC).strftime("%d %B")

    try:
        pulse, metrics = await generate_daily_pulse()

        if not pulse or len(pulse) < 50:
            log.warning("Empty pulse generated, skipping")
            return

        # Record metrics for anomaly detection baselines
        flat = flatten_metrics(metrics)
        if flat:
            metric_store.record_metrics(flat)
            log.debug("Recorded %d metrics to history", len(flat))

        # Prune old metrics (>90 days)
        metric_store.prune_old()

        # Count sources that worked vs failed
        ok = sum(1 for m in metrics.values() if "error" not in m)
        failed = sum(1 for m in metrics.values() if "error" in m)

        header = f"<b>\U0001f4ca Daily Pulse \u2014 {today}</b>\n\n"

        # Split if too long
        full_text = header + pulse
        if len(full_text) <= 4096:
            send_bot_api(full_text, parse_mode="HTML", topic_id=TOPIC_DIGEST)
        else:
            mid = len(pulse) // 2
            split_at = pulse.rfind("\n", 0, mid + 200)
            if split_at < mid - 200:
                split_at = mid
            send_bot_api(header + pulse[:split_at], parse_mode="HTML", topic_id=TOPIC_DIGEST)
            send_bot_api(pulse[split_at:], parse_mode="HTML", topic_id=TOPIC_DIGEST)

        log.info("Daily pulse sent: %d sources OK, %d failed", ok, failed)

    except Exception as e:
        log.error("Daily pulse failed: %s", e)
        send_bot_api(
            f"\u26a0\ufe0f Daily pulse failed: {str(e)[:200]}",
            topic_id=TOPIC_DIGEST,
        )
