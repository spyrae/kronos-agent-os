"""Weekly swarm post-mortem (moat phase 11.4).

The report is read-only aggregation, so the risk is not corruption — it is
plausible-looking wrong numbers. These tests pin the arithmetic against a
synthetic week, and pin that an empty ledger renders instead of raising.
"""

import time

import pytest

from kronos.config import settings
from kronos.swarm_report import PERIOD_DAYS, build_report, render_markdown, render_summary


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    import kronos.db as _db

    _db._instances.clear()
    from kronos.swarm_store import SwarmStore

    store = SwarmStore()
    monkeypatch.setattr("kronos.swarm_report.get_swarm", lambda: store)
    yield store
    _db._instances.clear()


def _reply(store, agent: str, tier: int, *, root: int, msg: int) -> None:
    store.claim_reply(
        chat_id=-100123,
        topic_id=7,
        root_msg_id=root,
        trigger_msg_id=msg,
        agent_name=agent,
        tier=tier,
        eta_ts=time.time(),
    )
    store.mark_sent(chat_id=-100123, topic_id=7, trigger_msg_id=msg, agent_name=agent, reply_msg_id=msg + 1000)


def _cost(store, agent: str, cost: float, requests: int = 10) -> None:
    store._db.write(
        """
        INSERT INTO swarm_costs (day, agent, requests, input_tokens, output_tokens, cost_usd, updated_at)
        VALUES (?, ?, ?, 0, 0, ?, ?)
        """,
        (time.strftime("%Y-%m-%d"), agent, requests, cost, time.time()),
    )


@pytest.fixture
def week(swarm):
    """A synthetic week with known numbers."""
    _reply(swarm, "kronos", 1, root=10, msg=10)
    _reply(swarm, "kronos", 2, root=11, msg=11)
    _reply(swarm, "nexus", 2, root=12, msg=12)
    _reply(swarm, "impulse", 3, root=12, msg=13)
    _cost(swarm, "kronos", 0.40)
    _cost(swarm, "nexus", 0.20)
    _cost(swarm, "impulse", 0.10)
    swarm.watch_sla(
        chat_id=-100123,
        topic_id=7,
        root_msg_id=11,
        thread_id="-100123:7",
        topic="planning",
        owner_agent="kronos",
        request="план",
        sla_minutes=15,
    )
    swarm.watch_sla(
        chat_id=-100123,
        topic_id=7,
        root_msg_id=14,
        thread_id="-100123:7",
        topic="metrics",
        owner_agent="nexus",
        request="почему упал DAU",
        sla_minutes=20,
    )
    planning = next(w for w in swarm.sla_watches() if w["topic"] == "planning")
    metrics = next(w for w in swarm.sla_watches() if w["topic"] == "metrics")
    swarm.resolve_sla_watch(planning["id"], state="answered")
    swarm.resolve_sla_watch(metrics["id"], state="escalated")
    handoff = swarm.create_handoff(
        chat_id=-100123,
        topic_id=7,
        thread_id="-100123:7",
        from_agent="impulse",
        to_agent="keystone",
        context="про архитектуру",
    )
    swarm.complete_handoff(handoff, success=True)
    challenge = swarm.request_challenge(
        chat_id=-100123,
        topic_id=7,
        thread_id="-100123:7",
        root_msg_id=11,
        topic="planning",
        author_agent="kronos",
        reviewer_agent="keystone",
        claim="Делаем A",
    )
    swarm.answer_challenge(challenge, verdict="challenge", response="Возражение: нет ресурсов")
    swarm.add_feedback(agent_name="kronos", chat_id=-100123, msg_id=1010, emoji="👍")
    swarm.add_feedback(agent_name="impulse", chat_id=-100123, msg_id=1013, emoji="👎")
    swarm.incr_metric("duplicate_replies_avoided", 3)
    return swarm


# --- arithmetic ---------------------------------------------------------------


def test_replies_are_counted_per_agent_and_tier(week):
    report = build_report("week")

    by_agent = {row["agent"]: row for row in report["agents"]}
    assert by_agent["kronos"]["replies"] == 2
    assert (by_agent["kronos"]["tier1"], by_agent["kronos"]["tier2"]) == (1, 1)
    assert by_agent["impulse"]["tier3"] == 1
    assert report["tiers"] == {
        "tier1": 1,
        "tier2": 2,
        "tier3": 1,
        "total": 4,
        "explicit_share": 25.0,
    }


def test_cost_per_reply_makes_agents_comparable(week):
    report = build_report("week")

    by_agent = {row["agent"]: row for row in report["agents"]}
    assert by_agent["kronos"]["cost_per_reply"] == 0.2  # $0.40 over two replies
    assert by_agent["impulse"]["cost_per_reply"] == 0.1
    assert report["totals"]["cost_usd"] == 0.7
    assert report["totals"]["cost_per_reply"] == 0.175


def test_agents_are_ordered_by_output(week):
    report = build_report("week")

    assert [row["agent"] for row in report["agents"]] == ["kronos", "impulse", "nexus"]


def test_an_agent_that_only_spent_still_appears(swarm):
    """Spending without answering is exactly what a report should surface."""
    _cost(swarm, "lacuna", 0.33)

    report = build_report("week")

    lacuna = report["agents"][0]
    assert lacuna["agent"] == "lacuna"
    assert lacuna["replies"] == 0
    assert lacuna["cost_per_reply"] is None, "dividing by zero replies would invent a number"


def test_ownership_reports_silence_per_topic(week):
    report = build_report("week")

    ownership = report["ownership"]
    assert (ownership["watched"], ownership["escalated"], ownership["pending"]) == (2, 1, 0)
    topics = {row["topic"]: row for row in ownership["topics"]}
    assert topics["metrics"]["escalated"] == 1
    assert topics["planning"]["answered"] == 1


def test_collaboration_counts_reviews_and_objections(week):
    report = build_report("week")

    collab = report["collaboration"]
    assert (collab["handoffs"], collab["handoffs_done"]) == (1, 1)
    assert (collab["reviews"], collab["objections"], collab["reviews_unanswered"]) == (1, 1, 0)


def test_feedback_is_attributed_per_agent(week):
    report = build_report("week")

    by_agent = {row["agent"]: row for row in report["agents"]}
    assert by_agent["kronos"]["feedback_positive"] == 1
    assert by_agent["impulse"]["feedback_negative"] == 1
    assert report["feedback"]["satisfaction_rate"] == 50.0


def test_metrics_are_passed_through(week):
    assert build_report("week")["metrics"]["duplicate_replies_avoided"] == 3


def test_a_shorter_window_excludes_older_rows(week, monkeypatch):
    """A day report must not inherit the week's replies."""
    report = build_report("day", now=time.time() + 3 * 86400)

    assert report["totals"]["replies"] == 0


@pytest.mark.parametrize("period", sorted(PERIOD_DAYS))
def test_every_period_builds(week, period):
    assert build_report(period)["period"] == period


def test_an_unknown_period_is_rejected(week):
    with pytest.raises(ValueError, match="unknown period"):
        build_report("fortnight")


# --- rendering ----------------------------------------------------------------


def test_markdown_carries_the_numbers(week):
    text = render_markdown(build_report("week"))

    assert "Отчёт роя" in text
    assert "| kronos | 2 |" in text
    assert "planning" in text and "metrics" in text
    assert "Ревью перед отправкой: 1" in text


def test_the_chat_summary_avoids_tables(week):
    """Telegram cannot render a markdown table, so the digest must not use one."""
    text = render_summary(build_report("week"))

    assert "|---" not in text
    assert "kronos" in text and "👍1" in text


def test_an_empty_ledger_renders_instead_of_raising(swarm):
    report = build_report("week")

    assert report["totals"]["replies"] == 0
    assert "Ответов за период не было." in render_markdown(report)
    assert render_summary(report)


# --- one delivery, not six ----------------------------------------------------


def test_only_one_agent_claims_the_week(swarm):
    assert swarm.claim_periodic_job(job="swarm-weekly-report", period_key="2026-W30", agent_name="kronos") is True
    assert swarm.claim_periodic_job(job="swarm-weekly-report", period_key="2026-W30", agent_name="nexus") is False
    assert swarm.periodic_job_owner(job="swarm-weekly-report", period_key="2026-W30") == "kronos"


def test_the_next_week_is_a_fresh_claim(swarm):
    swarm.claim_periodic_job(job="swarm-weekly-report", period_key="2026-W30", agent_name="kronos")

    assert swarm.claim_periodic_job(job="swarm-weekly-report", period_key="2026-W31", agent_name="nexus") is True


@pytest.mark.asyncio
async def test_the_weekly_job_sends_once_across_agents(week, monkeypatch):
    from kronos.cron import swarm_weekly

    monkeypatch.setattr("kronos.cron.swarm_weekly.get_swarm", lambda: week)
    sent: list[str] = []
    monkeypatch.setattr(swarm_weekly, "send_bot_api", lambda text, **kwargs: sent.append(text) or True)
    monkeypatch.setattr(swarm_weekly, "send_ntfy", lambda *args, **kwargs: True)

    await swarm_weekly.run_swarm_weekly_report()
    monkeypatch.setattr(settings, "agent_name", "nexus")
    await swarm_weekly.run_swarm_weekly_report()

    assert len(sent) == 1
    assert "Отчёт роя" in sent[0]


@pytest.mark.asyncio
async def test_a_quiet_week_is_not_pushed(swarm, monkeypatch):
    from kronos.cron import swarm_weekly

    monkeypatch.setattr("kronos.cron.swarm_weekly.get_swarm", lambda: swarm)
    sent: list[str] = []
    monkeypatch.setattr(swarm_weekly, "send_bot_api", lambda text, **kwargs: sent.append(text) or True)
    monkeypatch.setattr(swarm_weekly, "send_ntfy", lambda *args, **kwargs: True)

    await swarm_weekly.run_swarm_weekly_report()

    assert sent == [], "an empty week is not worth a notification"
