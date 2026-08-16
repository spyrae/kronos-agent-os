"""The daily sandbox probe.

Thin: probing lives in `kronos.tools.sandbox`, the rules about who hears what in
`kronos.health`. What this module owns is that only one of six agents does the
work, and which of two opposite things a failure means.
"""

import pytest

from kronos.config import settings
from kronos.cron import sandbox_smoke
from kronos.health import STATUS_BROKEN, STATUS_OK, HealthCheck
from kronos.tools.sandbox import CHECK_EXECUTION, CHECK_NO_NETWORK, CHECK_WORKSPACE


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_name", sandbox_smoke.OWNER_AGENT)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))

    sent: list[str] = []
    from kronos.cron import notify

    monkeypatch.setattr(notify, "send_webhook", lambda text, **kw: sent.append(text))
    monkeypatch.setattr(notify, "send_ntfy", lambda text, **kw: None)
    return sent


def probing(monkeypatch, **statuses):
    async def _check():
        return [HealthCheck(name, status, "detail") for name, status in statuses.items()]

    monkeypatch.setattr(sandbox_smoke, "check_sandbox_health", _check)


@pytest.mark.asyncio
async def test_only_the_owning_agent_probes(host, monkeypatch):
    """One daemon and one image per host: six probes are ten wasted containers."""
    monkeypatch.setattr(settings, "agent_name", "lacuna")
    called = []

    async def _check():
        called.append(True)
        return []

    monkeypatch.setattr(sandbox_smoke, "check_sandbox_health", _check)

    await sandbox_smoke.run_sandbox_smoke()

    assert called == []
    assert host == []


@pytest.mark.asyncio
async def test_losing_execution_is_reported_as_lost_capability(host, monkeypatch):
    """Nothing runs, so nothing unsafe runs either — there is no fallback path."""
    probing(monkeypatch, execution=STATUS_BROKEN)

    await sandbox_smoke.run_sandbox_smoke()

    assert "lost capability, not lost safety" in host[0]


@pytest.mark.asyncio
async def test_losing_containment_is_reported_as_the_opposite(host, monkeypatch):
    """Code still runs, with a wall down. Saying "no safety was lost" here would
    be exactly backwards, which is why the two messages are not one message."""
    probing(monkeypatch, execution=STATUS_OK, no_network=STATUS_BROKEN)

    await sandbox_smoke.run_sandbox_smoke()

    assert "one of its walls down" in host[0]
    assert "ENABLE_CODE_EXECUTION=false" in host[0], "the reader needs the off switch"
    assert "lost capability" not in host[0]


@pytest.mark.asyncio
async def test_a_broken_workspace_is_not_mistaken_for_a_breach(host, monkeypatch):
    """A mount that does not persist is lost capability, not a missing wall."""
    probing(monkeypatch, execution=STATUS_OK, workspace=STATUS_BROKEN)

    await sandbox_smoke.run_sandbox_smoke()

    assert "lost capability, not lost safety" in host[0]


@pytest.mark.asyncio
async def test_the_containment_names_are_the_ones_the_probe_reports(monkeypatch):
    """Guards the seam: a renamed check would silently stop counting as a breach."""
    from kronos.tools.sandbox import CONTAINMENT_CHECKS

    assert CHECK_NO_NETWORK in CONTAINMENT_CHECKS
    assert CHECK_EXECUTION not in CONTAINMENT_CHECKS
    assert CHECK_WORKSPACE not in CONTAINMENT_CHECKS


@pytest.mark.asyncio
async def test_state_lives_beside_this_agent_s_database(host, tmp_path, monkeypatch):
    probing(monkeypatch, execution=STATUS_OK)

    await sandbox_smoke.run_sandbox_smoke()

    assert sandbox_smoke._state_file() == tmp_path / "sandbox_health.json"
