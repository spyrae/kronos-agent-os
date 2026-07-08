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
