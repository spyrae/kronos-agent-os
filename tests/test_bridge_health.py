"""Live /health endpoint.

Guards the change from a static {"status": "ok"} to a real readiness probe
that reflects the Telegram client connection (so a 15-min health-check stops
validating a fiction).
"""

import json
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

from kronos import bridge


async def test_health_degraded_when_client_disconnected(monkeypatch):
    monkeypatch.setattr(bridge, "_client", None)
    resp = await bridge._handle_health(make_mocked_request("GET", "/health"))
    assert resp.status == 503
    assert json.loads(resp.body)["telegram_connected"] is False


async def test_health_ok_when_client_connected(monkeypatch):
    monkeypatch.setattr(bridge, "_client", SimpleNamespace(is_connected=lambda: True))
    resp = await bridge._handle_health(make_mocked_request("GET", "/health"))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["telegram_connected"] is True
    assert "providers_in_cooldown" in body
