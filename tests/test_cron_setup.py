from kronos.config import settings
from kronos.cron.setup import setup_cron_jobs


class _SchedulerSpy:
    def __init__(self) -> None:
        self.names: list[str] = []

    def add_periodic(self, name, _func, interval_seconds):
        self.names.append(name)

    def add_daily(self, name, _func, hour_utc):
        self.names.append(name)

    def add_weekly(self, name, _func, weekday, hour_utc):
        self.names.append(name)


def test_nexus_exclusive_reports_register_only_on_nexus(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "nexus")
    scheduler = _SchedulerSpy()

    setup_cron_jobs(scheduler)

    assert "competitor-weekly" in scheduler.names
    assert "analytics-pulse" in scheduler.names
    assert "analytics-alerts" in scheduler.names
    assert "analytics-weekly" in scheduler.names
    assert "seo-geo-weekly" in scheduler.names


def test_jb_exclusive_reports_do_not_register_on_kronos(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    scheduler = _SchedulerSpy()

    setup_cron_jobs(scheduler)

    assert "competitor-weekly" not in scheduler.names
    assert "analytics-pulse" not in scheduler.names
    assert "analytics-alerts" not in scheduler.names
    assert "analytics-weekly" not in scheduler.names
    assert "seo-geo-weekly" not in scheduler.names
