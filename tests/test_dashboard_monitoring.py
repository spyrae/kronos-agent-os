import json
from types import SimpleNamespace

import pytest

from dashboard.api import monitoring


def _job(**overrides):
    data = {
        "enabled": True,
        "_running": False,
        "last_run": 0.0,
        "interval_seconds": 300,
        "cron_hour": None,
        "cron_weekday": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_cost_history_aggregates_daily_values(tmp_path, monkeypatch):
    logs = tmp_path / "kaos" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(monitoring.settings, "db_path", str(tmp_path / "kaos" / "session.db"))
    (logs / "cost.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-04-26T10:00:00+00:00", "cost_usd": 0.0123}),
            json.dumps({"ts": "2026-04-26T11:00:00+00:00", "cost_usd": 0.0044}),
            json.dumps({"ts": "2026-04-27T10:00:00+00:00", "cost_usd": 0.02}),
        ]),
        encoding="utf-8",
    )

    result = await monitoring.get_cost_history(days=7)

    assert result["days"] == [
        {"date": "2026-04-26", "cost_usd": 0.0167, "requests": 2},
        {"date": "2026-04-27", "cost_usd": 0.02, "requests": 1},
    ]


@pytest.mark.asyncio
async def test_jobs_endpoint_reports_schedule_and_controls(monkeypatch):
    fake_scheduler = SimpleNamespace(jobs={
        "heartbeat": _job(last_run=1777284000.0),
        "news-monitor": _job(interval_seconds=None, cron_hour=0),
    })
    monkeypatch.setattr(monitoring, "_scheduler", fake_scheduler)
    monkeypatch.setattr(monitoring.settings, "agent_name", "kaos")

    result = await monitoring.get_jobs()
    jobs = {item["name"]: item for item in result["jobs"]}

    assert result["scheduler_attached"] is True
    assert jobs["heartbeat"]["schedule"] == "every 5m"
    assert jobs["heartbeat"]["safe_controls"]["trigger_now"] is True
    assert jobs["news-monitor"]["schedule"] == "daily 00:00 UTC"
    assert jobs["news-monitor"]["safe_controls"]["trigger_now"] is False


@pytest.mark.asyncio
async def test_pause_and_resume_job(monkeypatch):
    fake_scheduler = SimpleNamespace(jobs={"heartbeat": _job()})
    monkeypatch.setattr(monitoring, "_scheduler", fake_scheduler)

    paused = await monitoring.pause_job("heartbeat")
    resumed = await monitoring.resume_job("heartbeat")

    assert paused["job"]["enabled"] is False
    assert paused["job"]["status"] == "paused"
    assert resumed["job"]["enabled"] is True
    assert resumed["job"]["status"] == "enabled"


@pytest.mark.asyncio
async def test_job_history_filters_by_job(tmp_path, monkeypatch):
    logs = tmp_path / "kaos" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(monitoring.settings, "db_path", str(tmp_path / "kaos" / "session.db"))
    (logs / "cron_runs.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-04-27T10:00:00+00:00", "job": "heartbeat", "status": "ok", "duration_ms": 10}),
            json.dumps({"ts": "2026-04-27T11:00:00+00:00", "job": "news-monitor", "status": "error", "duration_ms": 20, "error": "boom"}),
        ]),
        encoding="utf-8",
    )

    result = await monitoring.get_job_history(job="news-monitor", status="all", limit=50)

    assert result["total"] == 1
    assert result["runs"][0]["status"] == "error"
    assert result["runs"][0]["error"] == "boom"
