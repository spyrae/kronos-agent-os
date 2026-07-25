"""Weekly swarm post-mortem, delivered once (moat 11.4).

Same report as `kaos swarm report --week`, pushed to the chat on Sunday so the
organisation gets reviewed without anyone remembering to ask.

All six agents run this scheduler, so the job first claims the week in the
shared ledger — the one whose INSERT won sends, the other five return. Without
that the digest would arrive six times.
"""

import asyncio
import logging
import time

from kronos.config import settings
from kronos.cron.notify import TOPIC_DIGEST, send_bot_api, send_ntfy
from kronos.swarm_report import build_report, render_summary
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.swarm_weekly")

JOB_NAME = "swarm-weekly-report"


def _week_key(now: float | None = None) -> str:
    """ISO year-week, so a retry inside the same week does not re-send."""
    return time.strftime("%G-W%V", time.gmtime(time.time() if now is None else now))


async def run_swarm_weekly_report() -> None:
    swarm = get_swarm()
    period_key = _week_key()

    if not swarm.claim_periodic_job(job=JOB_NAME, period_key=period_key, agent_name=settings.agent_name):
        owner = swarm.periodic_job_owner(job=JOB_NAME, period_key=period_key)
        log.info("Weekly swarm report for %s already claimed by %s", period_key, owner or "another agent")
        return

    report = build_report("week")
    if not report["totals"]["replies"] and not report["ownership"]["watched"]:
        log.info("Weekly swarm report: nothing happened this week, skipping the push")
        return

    body = render_summary(report)
    delivered = await asyncio.to_thread(send_bot_api, body, topic_id=TOPIC_DIGEST)
    if not delivered:
        log.warning("Weekly swarm report was not delivered to Telegram")

    totals = report["totals"]
    await asyncio.to_thread(
        send_ntfy,
        f"Ответов {totals['replies']}, расход ${totals['cost_usd']:.2f}, эскалаций {report['ownership']['escalated']}",
        title="Отчёт роя за неделю",
        tags="bar_chart",
    )
