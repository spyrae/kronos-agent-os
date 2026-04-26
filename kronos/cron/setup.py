"""Register all cron jobs with the scheduler.

Jobs are agent-aware: some only run on specific agents (e.g. competitor
monitoring only on nexus). The ``agent_name`` from settings controls this.
"""

import logging

from kronos.config import settings
from kronos.cron.scheduler import Scheduler

log = logging.getLogger("kronos.cron")

# Jobs that should only run on a specific agent to avoid duplicate work.
# Key: job name, value: agent_name that owns it.
_AGENT_EXCLUSIVE_JOBS: dict[str, str] = {
    "competitor-digest": "nexus",
    "competitor-weekly": "nexus",
    "competitor-alerts": "nexus",
    "analytics-pulse": "nexus",
    "analytics-weekly": "nexus",
    "analytics-alerts": "nexus",
}


def setup_cron_jobs(scheduler: Scheduler) -> None:
    """Register all cron jobs. Matches Kronos I systemd timers."""

    from kronos.cron.heartbeat import run_heartbeat
    from kronos.cron.news_monitor import run_news_monitor
    from kronos.cron.self_improve import run_self_improve
    from kronos.cron.skill_improve import run_skill_improve
    from kronos.cron.people_scout import run_people_scout
    from kronos.cron.user_model import run_user_model
    from kronos.cron.group_digest import run_group_digest
    from kronos.cron.sleep_compute import run_sleep_compute
    from kronos.cron.expense_digest import run_expense_digest
    from kronos.cron.market_review import run_market_review
    from kronos.cron.email_expenses import run_email_expenses
    from kronos.cron.swarm_retention import run_swarm_retention
    from kronos.cron.competitor_digest import run_competitor_digest
    from kronos.cron.competitor_weekly import run_competitor_weekly
    from kronos.cron.competitor_alerts import run_competitor_alerts
    from kronos.cron.analytics_pulse import run_analytics_pulse
    from kronos.cron.analytics_weekly import run_analytics_weekly
    from kronos.cron.analytics_alerts import run_analytics_alerts

    me = settings.agent_name

    # Heartbeat — every 30 minutes (was: kronos-heartbeat.timer)
    scheduler.add_periodic("heartbeat", run_heartbeat, interval_seconds=1800)

    # News Monitor — daily at 00:30 UTC (was: kronos-news-monitor.timer)
    scheduler.add_daily("news-monitor", run_news_monitor, hour_utc=0)

    # Group Digest — daily at 01:00 UTC (09:00 UTC+8)
    scheduler.add_daily("group-digest", run_group_digest, hour_utc=1)

    # Self-Improve — daily at 22:00 UTC (was: kronos-self-improve.timer)
    scheduler.add_daily("self-improve", run_self_improve, hour_utc=22)

    # Skill Improve — weekly Sunday 20:00 UTC (was: kronos-skill-improve.timer)
    scheduler.add_weekly("skill-improve", run_skill_improve, weekday=6, hour_utc=20)

    # People Scout — weekly Sunday 02:00 UTC (was: kronos-people-scout.timer)
    scheduler.add_weekly("people-scout", run_people_scout, weekday=6, hour_utc=2)

    # User Model — weekly Wednesday 20:00 UTC (was: kronos-user-model.timer)
    scheduler.add_weekly("user-model", run_user_model, weekday=2, hour_utc=20)

    # Sleep-time Compute — daily at 03:00 UTC (11:00 UTC+8, after user sleeps)
    scheduler.add_daily("sleep-compute", run_sleep_compute, hour_utc=3)

    # Email Expenses — daily at 00:00 UTC (08:00 UTC+8)
    scheduler.add_daily("email-expenses", run_email_expenses, hour_utc=0)

    # Expense Digest — weekly Sunday 02:00 UTC (10:00 UTC+8)
    scheduler.add_weekly("expense-digest", run_expense_digest, weekday=6, hour_utc=2)

    # Market Review — weekly Friday 10:00 UTC (18:00 UTC+8)
    scheduler.add_weekly("market-review", run_market_review, weekday=4, hour_utc=10)

    # Swarm Retention — weekly Sunday 03:00 UTC. Prunes swarm_messages
    # older than MESSAGE_RETENTION_DAYS (90d). Safe on all 6 agents.
    scheduler.add_weekly("swarm-retention", run_swarm_retention, weekday=6, hour_utc=3)

    # ── Agent-exclusive jobs (only registered on the owning agent) ──────
    nexus_jobs_registered = 0

    if _AGENT_EXCLUSIVE_JOBS.get("competitor-digest") == me:
        scheduler.add_daily("competitor-digest", run_competitor_digest, hour_utc=8)
        scheduler.add_weekly("competitor-weekly", run_competitor_weekly, weekday=6, hour_utc=10)
        scheduler.add_periodic("competitor-alerts", run_competitor_alerts, interval_seconds=14400)
        scheduler.add_daily("analytics-pulse", run_analytics_pulse, hour_utc=8)
        scheduler.add_weekly("analytics-weekly", run_analytics_weekly, weekday=0, hour_utc=9)
        scheduler.add_periodic("analytics-alerts", run_analytics_alerts, interval_seconds=7200)
        nexus_jobs_registered = 6

    total = 12 + nexus_jobs_registered
    log.info("%d cron jobs registered for agent '%s'", total, me)
