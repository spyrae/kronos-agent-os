"""Fail-closed auth for the bridge webhook server.

Regression guard for the insecure default where an empty webhook_secret
turned auth off (`"" == ""`) on a network-bound server, exposing /webhook
and the chat-dumping /history.
"""

from aiohttp.test_utils import make_mocked_request

from kronos import bridge
from kronos.config import settings


def _req(secret_header: str | None = None):
    headers = {"X-Webhook-Secret": secret_header} if secret_header is not None else {}
    return make_mocked_request("POST", "/webhook", headers=headers)


def test_empty_secret_is_fail_closed(monkeypatch):
    # No secret configured → the endpoint must reject everyone, never open up.
    monkeypatch.setattr(settings, "webhook_secret", "")
    assert bridge._webhook_unauthorized(_req()) is not None
    assert bridge._webhook_unauthorized(_req("")) is not None
    assert bridge._webhook_unauthorized(_req("anything")) is not None
    assert bridge._webhook_unauthorized(_req()).status == 401


def test_correct_secret_authorizes(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    assert bridge._webhook_unauthorized(_req("s3cret")) is None


def test_wrong_or_missing_secret_rejected(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    assert bridge._webhook_unauthorized(_req("wrong")) is not None
    assert bridge._webhook_unauthorized(_req()) is not None
    assert bridge._webhook_unauthorized(_req("")) is not None
