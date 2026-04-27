import pytest

from dashboard.api import health


@pytest.mark.asyncio
async def test_health_uses_configured_agent_name(monkeypatch):
    monkeypatch.setattr(health.settings, "agent_name", "demo")

    result = await health.health()

    assert result["status"] == "ok"
    assert result["agent"] == "demo"
    assert isinstance(result["uptime_seconds"], int)
