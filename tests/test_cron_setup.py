import sys
from types import SimpleNamespace

import pytest

from kronos.config import settings
from kronos.cron.competitor_weekly import run_competitor_weekly
from kronos.cron.setup import setup_cron_jobs
from kronos.cron.signal_travel import run_travel_insights_digest


class _SchedulerSpy:
    def __init__(self) -> None:
        self.names: list[str] = []

    def add_periodic(self, name, _func, interval_seconds):
        self.names.append(name)

    def add_daily(self, name, _func, hour_utc):
        self.names.append(name)

    def add_weekly(self, name, _func, weekday, hour_utc):
        self.names.append(name)


def test_nexus_exclusive_reports_register_only_enabled_jobs_on_nexus(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "nexus")
    scheduler = _SchedulerSpy()

    setup_cron_jobs(scheduler)

    assert "competitor-weekly" not in scheduler.names
    assert "analytics-pulse" in scheduler.names
    assert "analytics-alerts" in scheduler.names
    assert "analytics-weekly" in scheduler.names
    assert "seo-geo-weekly" in scheduler.names


def test_jb_exclusive_reports_do_not_register_on_kronos(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    scheduler = _SchedulerSpy()

    setup_cron_jobs(scheduler)

    assert "competitor-weekly" not in scheduler.names
    assert "signal-travel-insights" not in scheduler.names
    assert "analytics-pulse" not in scheduler.names
    assert "analytics-alerts" not in scheduler.names
    assert "analytics-weekly" not in scheduler.names
    assert "seo-geo-weekly" not in scheduler.names


@pytest.mark.asyncio
async def test_disabled_competitor_runner_does_not_collect_or_publish(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "nexus")

    async def fail_generate():
        raise AssertionError("competitor report should not be generated")

    monkeypatch.setitem(
        sys.modules,
        "kronos.competitors.weekly_report",
        SimpleNamespace(generate_weekly_report=fail_generate),
    )

    await run_competitor_weekly()


@pytest.mark.asyncio
async def test_disabled_travel_runner_does_not_collect_or_publish(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")

    async def fail_run_signal_digest(*_args, **_kwargs):
        raise AssertionError("travel insights should not be collected")

    monkeypatch.setitem(
        sys.modules,
        "kronos.signals.pipeline",
        SimpleNamespace(run_signal_digest=fail_run_signal_digest),
    )

    await run_travel_insights_digest()
