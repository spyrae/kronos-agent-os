"""Register all cron jobs with the scheduler.

Jobs are agent-aware: some only run on specific agents (e.g. analytics
reports only on nexus). The ``agent_name`` from settings controls this.
"""

import logging

from kronos.config import settings
from kronos.cron.scheduler import Scheduler

log = logging.getLogger("kronos.cron")

# Jobs that should only run on a specific agent to avoid duplicate work.
# Key: job name, value: agent_name that owns it.
_AGENT_EXCLUSIVE_JOBS: dict[str, str] = {
    "analytics-pulse": "nexus",
    "analytics-weekly": "nexus",
    "analytics-alerts": "nexus",
}


def setup_cron_jobs(scheduler: Scheduler) -> None:
    """Register all cron jobs. Matches Kronos I systemd timers."""

    from kronos.cron.analytics_alerts import run_analytics_alerts
    from kronos.cron.analytics_pulse import run_analytics_pulse
    from kronos.cron.analytics_weekly import run_analytics_weekly
    from kronos.cron.expense_digest import run_expense_digest
    from kronos.cron.expenses.processor import run_email_expenses

    # DISABLED 2026-07-07: group-digest paused — duplicate of news-monitor on Digest:News.
    # from kronos.cron.group_digest import run_group_digest
    from kronos.cron.heartbeat import run_heartbeat
    from kronos.cron.market_review import run_market_review
    from kronos.cron.news_monitor import run_news_monitor
    from kronos.cron.personal_observer import run_daily_scope, run_personal_observer
    from kronos.cron.reminders import run_due_reminders
    from kronos.cron.self_improve import run_self_improve
    from kronos.cron.signal_ideas import run_ideas_digest

    # DISABLED 2026-06-11: job-search digest paused (see registration below).
    # from kronos.cron.signal_jobs import run_jobs_digest
    # DISABLED 2026-07-05: People Scout paused (see registration below).
    # from kronos.cron.people_scout import run_people_scout
    # DISABLED 2026-07-03: JourneyBay travel insights collection/analysis/
    # publication paused (see registration below).
    # from kronos.cron.signal_travel import run_travel_insights_digest
    from kronos.cron.skill_improve import run_skill_improve
    from kronos.cron.sleep_compute import run_sleep_compute
    from kronos.cron.source_quality_audit import run_source_quality_audit
    from kronos.cron.swarm_retention import run_swarm_retention
    from kronos.cron.user_model import run_user_model

    me = settings.agent_name

    # Heartbeat — every 30 minutes (was: kronos-heartbeat.timer)
    scheduler.add_periodic("heartbeat", run_heartbeat, interval_seconds=1800)

    # User-scheduled reminders / tasks — poll every minute (roadmap 4.2)
    scheduler.add_periodic("user-reminders", run_due_reminders, interval_seconds=60)

    # News Monitor — daily at 00:00 UTC (was: kronos-news-monitor.timer)
    scheduler.add_daily("news-monitor", run_news_monitor, hour_utc=0)

    # Personal Observer — daily at 23:00 UTC (07:00 UTC+8), avoids 00:00/01:00 digest conflicts.
    scheduler.add_daily("personal-observer", run_personal_observer, hour_utc=23)

    # Group Digest — daily at 01:00 UTC (09:00 UTC+8).
    # DISABLED 2026-07-07: paused — news-monitor (Signal Intelligence) already
    # publishes to the same Digest:News topic at 00:00 UTC, so group-digest at
    # 01:00 produced a second, duplicate message. GROUPS.md Telegram sources are
    # kept intact for a future merge into Signal Intelligence. To re-enable:
    # uncomment the import above + this line, bump the job count below back to
    # 15, then restart the kronos agent.
    # scheduler.add_daily("group-digest", run_group_digest, hour_utc=1)

    # Jobs Digest — daily at 02:00 UTC (dedicated Signal Intelligence topic).
    # DISABLED 2026-06-11: paused — job-search signals are not being collected
    # for now. The pipeline, config fields and Telegram topic stay intact.
    # To re-enable: uncomment the import above + this line, bump the job count
    # below back to 16, then restart the kronos agent.
    # scheduler.add_daily("signal-jobs", run_jobs_digest, hour_utc=2)

    # Product/Business Ideas — daily at 04:00 UTC (dedicated topic)
    scheduler.add_daily("signal-ideas", run_ideas_digest, hour_utc=4)

    # JourneyBay Travel Insights — daily at 05:00 UTC (dedicated topic).
    # DISABLED 2026-07-03: stop all collection/analysis/publication for now.
    # To re-enable: uncomment the import above + this line, bump the job count
    # below back to 17, then restart the kronos agent.
    # scheduler.add_daily("signal-travel-insights", run_travel_insights_digest, hour_utc=5)

    # Daily Scope — daily at 14:00 UTC (22:00 UTC+8)
    scheduler.add_daily("daily-scope", run_daily_scope, hour_utc=14)

    # Self-Improve — daily at 22:00 UTC (was: kronos-self-improve.timer)
    scheduler.add_daily("self-improve", run_self_improve, hour_utc=22)

    # Skill Improve — weekly Sunday 20:00 UTC (was: kronos-skill-improve.timer)
    scheduler.add_weekly("skill-improve", run_skill_improve, weekday=6, hour_utc=20)

    # People Scout — weekly Sunday 02:00 UTC (was: kronos-people-scout.timer).
    # DISABLED 2026-07-05: paused — LinkedIn profile discovery is not needed for
    # now. The runner, focus rotation, criteria and SEEN.md tracking stay intact.
    # To re-enable: uncomment the import above + this line, flip
    # PEOPLE_SCOUT_ENABLED back to True in people_scout.py, bump the job count
    # below back to 16, then restart the kronos agent.
    # scheduler.add_weekly("people-scout", run_people_scout, weekday=6, hour_utc=2)

    # User Model — weekly Wednesday 20:00 UTC (was: kronos-user-model.timer)
    scheduler.add_weekly("user-model", run_user_model, weekday=2, hour_utc=20)

    # Sleep-time Compute — daily at 03:00 UTC (11:00 UTC+8, after user sleeps)
    scheduler.add_daily("sleep-compute", run_sleep_compute, hour_utc=3)

    # Email Expenses — daily at 13:00 UTC (21:00 UTC+8, вечером за прошедший день)
    scheduler.add_daily("email-expenses", run_email_expenses, hour_utc=13)

    # Expense Digest — weekly Sunday 02:00 UTC (10:00 UTC+8)
    scheduler.add_weekly("expense-digest", run_expense_digest, weekday=6, hour_utc=2)

    # Market Review — weekly Friday 10:00 UTC (18:00 UTC+8)
    scheduler.add_weekly("market-review", run_market_review, weekday=4, hour_utc=10)

    # Source Quality Audit — weekly check with a 13-day guard = biweekly cadence.
    scheduler.add_weekly("source-quality-audit", run_source_quality_audit, weekday=6, hour_utc=4)

    # Swarm Retention — weekly Sunday 03:00 UTC. Prunes swarm_messages
    # older than MESSAGE_RETENTION_DAYS (90d). Safe on all 6 agents.
    scheduler.add_weekly("swarm-retention", run_swarm_retention, weekday=6, hour_utc=3)

    # ── Agent-exclusive jobs (only registered on the owning agent) ──────
    nexus_jobs_registered = 0

    if _AGENT_EXCLUSIVE_JOBS.get("analytics-weekly") == me:
        from kronos.cron.seo_geo_check import run_seo_geo_weekly

        # Daily analytics pulse at 01:00 UTC = 04:00 MSK — before user wakes up.
        scheduler.add_daily("analytics-pulse", run_analytics_pulse, hour_utc=1)
        # Periodic anomaly detector — every 2h.
        scheduler.add_periodic("analytics-alerts", run_analytics_alerts, interval_seconds=7200)

        # ── Weekly reports land Monday morning MSK ──
        # Spaced 3h apart so the LLM (one Codex/DeepSeek process) is never
        # contended and Telegram doesn't get a burst of giant messages.
        #   03:00 UTC (06:00 MSK) — SEO/GEO (longest: 25-35 min run)
        #   06:00 UTC (09:00 MSK) — Competitor intelligence (paused)
        #   09:00 UTC (12:00 MSK) — Analytics business report
        scheduler.add_weekly("seo-geo-weekly", run_seo_geo_weekly, weekday=0, hour_utc=3)
        # DISABLED 2026-07-03: stop all competitor collection/analysis/publication
        # for JB: Competitors Status. To re-enable: import
        # run_competitor_weekly above, uncomment this line, bump
        # nexus_jobs_registered back to 5, then restart the nexus agent.
        # scheduler.add_weekly("competitor-weekly", run_competitor_weekly, weekday=0, hour_utc=6)
        scheduler.add_weekly("analytics-weekly", run_analytics_weekly, weekday=0, hour_utc=9)
        nexus_jobs_registered = 4

    total = 15 + nexus_jobs_registered  # +user-reminders; signal-jobs, travel insights, people-scout and group-digest paused
    log.info("%d cron jobs registered for agent '%s'", total, me)
