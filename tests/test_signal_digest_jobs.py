"""The weekly digest jobs must ask the pipeline for a week of signal."""

import sys
from types import SimpleNamespace

import pytest

from kronos.config import settings
from kronos.signals.pipeline import WEEKLY_FETCH_LIMIT, WEEKLY_FRESHNESS, WEEKLY_LOOKBACK_HOURS


def _capturing_pipeline(calls):
    async def run_signal_digest(category, **kwargs):
        calls.append((category, kwargs))
        return SimpleNamespace(sent=True, saved_item_count=1, cluster_count=1, rendered=SimpleNamespace(body="x"))

    return SimpleNamespace(
        run_signal_digest=run_signal_digest,
        WEEKLY_FETCH_LIMIT=WEEKLY_FETCH_LIMIT,
        WEEKLY_FRESHNESS=WEEKLY_FRESHNESS,
        WEEKLY_LOOKBACK_HOURS=WEEKLY_LOOKBACK_HOURS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "runner", "category"),
    [
        ("kronos.cron.news_monitor", "run_news_monitor", "news"),
        ("kronos.cron.signal_ideas", "run_ideas_digest", "ideas"),
    ],
)
async def test_digest_job_requests_the_weekly_window(monkeypatch, module, runner, category):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    calls = []
    monkeypatch.setitem(sys.modules, "kronos.signals.pipeline", _capturing_pipeline(calls))

    import importlib

    await getattr(importlib.import_module(module), runner)()

    assert len(calls) == 1
    got_category, kwargs = calls[0]
    assert got_category == category
    assert kwargs["fetch_limit"] == WEEKLY_FETCH_LIMIT
    assert kwargs["freshness"] == WEEKLY_FRESHNESS
    assert kwargs["lookback_hours"] == WEEKLY_LOOKBACK_HOURS
