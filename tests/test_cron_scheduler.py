"""Cron scheduler resilience: failure alerting + counter bookkeeping.

Guards the change that alerts via NTFY after N consecutive failures instead
of letting a job fail silently for a week.
"""

from unittest.mock import patch

import pytest

from kronos.cron.scheduler import CRON_ALERT_THRESHOLD, CronJob, Scheduler


@pytest.fixture
def scheduler(monkeypatch):
    # Keep _run_job off the real data/ and logs/ paths.
    monkeypatch.setattr("kronos.cron.scheduler._append_run_history", lambda entry: None)
    sched = Scheduler()
    monkeypatch.setattr(sched, "_save_state", lambda: None)
    return sched


async def test_alert_fires_once_at_threshold(scheduler):
    async def flaky():
        raise RuntimeError("boom")

    job = CronJob(name="flaky", func=flaky)
    with patch("kronos.cron.notify.send_ntfy", return_value=True) as ntfy:
        for _ in range(CRON_ALERT_THRESHOLD + 2):
            await scheduler._run_job(job)

    assert job.consecutive_failures == CRON_ALERT_THRESHOLD + 2
    ntfy.assert_called_once()  # exactly once, at the threshold — no spam after


async def test_no_alert_below_threshold(scheduler):
    async def flaky():
        raise RuntimeError("boom")

    job = CronJob(name="flaky2", func=flaky)
    with patch("kronos.cron.notify.send_ntfy", return_value=True) as ntfy:
        for _ in range(CRON_ALERT_THRESHOLD - 1):
            await scheduler._run_job(job)

    ntfy.assert_not_called()


async def test_success_resets_failure_counter(scheduler):
    state = {"fail": True}

    async def sometimes():
        if state["fail"]:
            raise RuntimeError("boom")

    job = CronJob(name="sometimes", func=sometimes)
    with patch("kronos.cron.notify.send_ntfy", return_value=True):
        await scheduler._run_job(job)
        assert job.consecutive_failures == 1
        state["fail"] = False
        await scheduler._run_job(job)
        assert job.consecutive_failures == 0


def test_cron_state_is_per_agent_and_roundtrips(tmp_path, monkeypatch):
    # Cron state lives next to the agent's own session DB (per-agent), not in a
    # swarm-shared data/cron_state.json, and survives a reload.
    from kronos.config import settings
    from kronos.cron import scheduler as sched_mod

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "agent" / "session.db"))
    assert sched_mod._state_file() == tmp_path / "agent" / "cron_state.json"

    async def _noop():
        return None

    s1 = Scheduler()
    s1.add(CronJob(name="job-a", func=_noop, interval_seconds=60))
    s1.jobs["job-a"].last_run = 123.0
    s1._save_state()

    # A fresh scheduler for the same agent restores the timestamp.
    s2 = Scheduler()
    s2.add(CronJob(name="job-a", func=_noop, interval_seconds=60))
    s2._load_state()
    assert s2.jobs["job-a"].last_run == 123.0
