"""The daily job that notices a fetch tier died.

Thin by design: probing lives in `kronos.tools.acquire` and the rules about who
hears what live in `kronos.health` (tested in test_health_report.py). What is
left here is the wiring — and the one decision this module owns, which is that
only one of six agents does the work.
"""

import json

import pytest

from kronos.config import settings
from kronos.cron import acquire_smoke
from kronos.health import STATUS_BROKEN, STATUS_OK, HealthCheck


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_name", acquire_smoke.OWNER_AGENT)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))

    sent: list[str] = []
    from kronos.cron import notify

    monkeypatch.setattr(notify, "send_webhook", lambda text, **kw: sent.append(text))
    monkeypatch.setattr(notify, "send_ntfy", lambda text, **kw: None)
    return sent


def tiers(monkeypatch, **statuses):
    async def _check(url=None):
        return [HealthCheck(name, status, "detail") for name, status in statuses.items()]

    monkeypatch.setattr(acquire_smoke, "check_tier_health", _check)


@pytest.mark.asyncio
async def test_only_the_owning_agent_probes(host, monkeypatch):
    """Six agents share one host: six probes are five wasted browser launches."""
    monkeypatch.setattr(settings, "agent_name", "impulse")
    called = []

    async def _check(url=None):
        called.append(True)
        return []

    monkeypatch.setattr(acquire_smoke, "check_tier_health", _check)

    await acquire_smoke.run_acquire_smoke()

    assert called == []
    assert host == []


@pytest.mark.asyncio
async def test_a_broken_tier_is_reported_and_remembered(host, tmp_path, monkeypatch):
    tiers(monkeypatch, plain=STATUS_OK, stealth=STATUS_BROKEN)

    await acquire_smoke.run_acquire_smoke()

    assert "stealth: broken" in host[0]
    assert "reported as unreadable" in host[0], "the consequence should be spelled out"
    assert json.loads(acquire_smoke._state_file().read_text())["stealth"] == STATUS_BROKEN


@pytest.mark.asyncio
async def test_state_lives_beside_this_agent_s_database(host, tmp_path, monkeypatch):
    """Not in swarm.db: this is a fact about one host's install, not shared knowledge."""
    tiers(monkeypatch, plain=STATUS_OK)

    await acquire_smoke.run_acquire_smoke()

    assert acquire_smoke._state_file() == tmp_path / "acquire_health.json"
