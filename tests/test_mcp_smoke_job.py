"""The daily MCP probe.

Thin: probing lives in `kronos.tools.manager`, the rules about who hears what in
`kronos.health`. What this module owns is that only one of six agents starts
eleven servers, and that the report says what a failure actually costs.
"""

import pytest

from kronos.config import settings
from kronos.cron import mcp_smoke
from kronos.health import STATUS_BROKEN, STATUS_OK, HealthCheck


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_name", mcp_smoke.OWNER_AGENT)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))

    sent: list[str] = []
    from kronos.cron import notify

    monkeypatch.setattr(notify, "send_webhook", lambda text, **kw: sent.append(text))
    monkeypatch.setattr(notify, "send_ntfy", lambda text, **kw: None)
    return sent


def probing(monkeypatch, **statuses):
    async def _check():
        return [HealthCheck(name, status, "detail") for name, status in statuses.items()]

    monkeypatch.setattr(mcp_smoke, "check_mcp_health", _check)


@pytest.mark.asyncio
async def test_only_the_owning_agent_probes(host, monkeypatch):
    """Six agents starting eleven servers each is sixty-six subprocesses."""
    monkeypatch.setattr(settings, "agent_name", "resonant")
    called = []

    async def _check():
        called.append(True)
        return []

    monkeypatch.setattr(mcp_smoke, "check_mcp_health", _check)

    await mcp_smoke.run_mcp_smoke()

    assert called == []
    assert host == []


@pytest.mark.asyncio
async def test_a_broken_server_is_reported_with_what_it_costs(host, monkeypatch):
    """ "yahoo-finance: broken" alone does not tell you the finance agent is blind."""
    probing(monkeypatch, fetch=STATUS_OK, yahoo_finance=STATUS_BROKEN)

    await mcp_smoke.run_mcp_smoke()

    assert "yahoo_finance: broken" in host[0]
    assert "absent from the agent" in host[0]
    assert "kaos mcp check" in host[0], "the reader needs the next step"


@pytest.mark.asyncio
async def test_silence_when_nothing_moved(host, monkeypatch):
    probing(monkeypatch, fetch=STATUS_OK)
    await mcp_smoke.run_mcp_smoke()
    host.clear()

    await mcp_smoke.run_mcp_smoke()

    assert host == []


@pytest.mark.asyncio
async def test_state_lives_beside_this_agent_s_database(host, tmp_path, monkeypatch):
    probing(monkeypatch, fetch=STATUS_OK)

    await mcp_smoke.run_mcp_smoke()

    assert mcp_smoke._state_file() == tmp_path / "mcp_health.json"
