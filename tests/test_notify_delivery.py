"""Notifications must reach an open topic, and a refused one must not vanish.

A forum that closes its built-in General topic refuses every message posted to
the group without a thread id. The health check, personal-observer and
daily-scope all posted that way, so their alerts were refused for days with an
ERROR line per attempt as the only trace.
"""

import io
import logging
import urllib.request

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telethon import errors
from telethon.tl import types

from kronos import bridge
from kronos.config import settings
from kronos.cron import notify


@pytest.fixture(autouse=True)
def _fresh_reports(monkeypatch):
    monkeypatch.setattr(notify, "_undelivered_reports", {})


def _telethon_error(code: int, message: str) -> Exception:
    return errors.rpc_message_to_error(types.RpcError(code, message), request=None)


# --- which refusals count as a broken destination ---


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ('{"ok":false,"error_code":400,"description":"Bad Request: TOPIC_CLOSED"}', "TOPIC_CLOSED"),
        ('{"ok":false,"error_code":400,"description":"Bad Request: message thread not found"}', "TOPIC_NOT_FOUND"),
        (
            '{"ok":false,"error_code":403,"description":"Forbidden: bot was kicked from the supergroup chat"}',
            "BOT_REMOVED",
        ),
    ],
)
def test_bot_api_refusals_that_retrying_cannot_fix(error, expected):
    assert notify.permanent_delivery_reason(error) == expected


def test_telethon_refusals_are_recognised_by_code_and_by_class():
    # Telethon has no dedicated class for TOPIC_CLOSED — it arrives as a plain BadRequestError.
    assert notify.permanent_delivery_reason(_telethon_error(400, "TOPIC_CLOSED")) == "TOPIC_CLOSED"
    assert notify.permanent_delivery_reason(_telethon_error(400, "TOPIC_DELETED")) == "TOPIC_DELETED"
    assert notify.permanent_delivery_reason(_telethon_error(403, "CHAT_WRITE_FORBIDDEN")) == "CHAT_WRITE_FORBIDDEN"


def test_transient_failures_are_not_mistaken_for_a_broken_destination():
    assert notify.permanent_delivery_reason(_telethon_error(420, "FLOOD_WAIT_30")) is None
    assert notify.permanent_delivery_reason(TimeoutError("timed out")) is None
    assert notify.permanent_delivery_reason('{"description":"Too Many Requests: retry after 5"}') is None


# --- one loud report instead of a line per attempt ---


def test_a_broken_destination_is_reported_once_per_window_with_the_repeat_count(monkeypatch, caplog):
    pushes = []
    monkeypatch.setattr(notify, "send_ntfy", lambda text, **kwargs: pushes.append((text, kwargs)) or True)
    caplog.set_level(logging.DEBUG, logger="kronos.cron.notify")
    window = notify.UNDELIVERED_REPORT_INTERVAL_SECONDS
    where = "chat -100 (General)"

    # A report at clock zero is still a first report — monotonic time starts low on a fresh host.
    assert notify.report_undelivered(where, "TOPIC_CLOSED", "Disk usage critical: 93%", now=0.0)
    assert not notify.report_undelivered(where, "TOPIC_CLOSED", "Disk usage critical: 93%", now=900.0)
    assert not notify.report_undelivered(where, "TOPIC_CLOSED", "Disk usage critical: 93%", now=window - 1)
    assert notify.report_undelivered(where, "TOPIC_CLOSED", "Disk usage critical: 92%", now=float(window))

    errors_logged = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors_logged) == 2
    assert where in errors_logged[0] and "TOPIC_CLOSED" in errors_logged[0]
    assert "2 more refused since the last report" in errors_logged[1]
    assert [kwargs["title"] for _, kwargs in pushes] == ["Kronos Agent OS: Telegram TOPIC_CLOSED"] * 2
    assert "Disk usage critical: 92%" in pushes[1][0]


def test_each_broken_destination_is_reported_on_its_own(monkeypatch):
    monkeypatch.setattr(notify, "send_ntfy", lambda text, **kwargs: True)

    assert notify.report_undelivered("chat -100 (General)", "TOPIC_CLOSED", "a", now=0.0)
    assert notify.report_undelivered("chat -100 topic 23", "TOPIC_CLOSED", "b", now=1.0)
    assert notify.report_undelivered("chat -100 (General)", "CHAT_WRITE_FORBIDDEN", "c", now=2.0)


# --- Bot API path (cron reports) ---


def _capture_bot_api(monkeypatch):
    sent = []
    monkeypatch.setattr(notify.settings, "tg_bot_token", "test-token")
    monkeypatch.setattr(notify, "_send_message", lambda url, body: sent.append(body) or True)
    return sent


def test_a_report_without_a_destination_goes_to_the_general_notifications_topic(monkeypatch):
    sent = _capture_bot_api(monkeypatch)
    monkeypatch.setattr(notify, "TOPIC_GENERAL", 23)

    notify.send_bot_api("daily scope", parse_mode="")

    assert sent[0]["message_thread_id"] == 23


def test_an_explicit_chat_or_topic_is_left_as_addressed(monkeypatch):
    sent = _capture_bot_api(monkeypatch)
    monkeypatch.setattr(notify, "TOPIC_GENERAL", 23)

    notify.send_bot_api("to a dm", chat_id=12345, parse_mode="")
    notify.send_bot_api("to ideas", topic_id=326, parse_mode="")

    assert "message_thread_id" not in sent[0]
    assert sent[1]["message_thread_id"] == 326


def test_without_a_general_topic_configured_the_old_behaviour_stays(monkeypatch):
    sent = _capture_bot_api(monkeypatch)
    monkeypatch.setattr(notify, "TOPIC_GENERAL", 0)

    notify.send_bot_api("daily scope", parse_mode="")

    assert "message_thread_id" not in sent[0]


def test_a_bot_api_refusal_is_reported_not_just_logged(monkeypatch):
    reports = []
    monkeypatch.setattr(
        notify, "report_undelivered", lambda where, reason, text: reports.append((where, reason, text)) or True
    )
    payload = b'{"ok":false,"error_code":400,"description":"Bad Request: TOPIC_CLOSED"}'

    def refuse(request, timeout):
        raise urllib.request.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(payload))

    monkeypatch.setattr(notify.urllib.request, "urlopen", refuse)

    ok = notify._send_message("https://api.telegram.org/bot0/sendMessage", {"chat_id": -100, "text": "daily scope"})

    assert ok is False
    assert reports == [("chat -100 (General)", "TOPIC_CLOSED", "daily scope")]


# --- webhook path (health check, alerts) ---


async def _post_webhook(monkeypatch, payload, send):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    monkeypatch.setattr(bridge, "_send_to_chat", send)
    monkeypatch.setattr(bridge, "DEFAULT_NOTIFY_CHAT", -100)
    app = web.Application()
    app.router.add_post("/webhook", bridge._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhook", json=payload, headers={"X-Webhook-Secret": "s3cret"})
        return response.status, await response.json()


async def test_a_text_only_webhook_post_goes_to_the_general_notifications_topic(monkeypatch):
    calls = []

    async def send(chat_id, text, parse_mode=None, topic_id=None):
        calls.append((chat_id, topic_id))

    monkeypatch.setenv("TOPIC_GENERAL", "23")

    status, _ = await _post_webhook(monkeypatch, {"text": "Kronos Agent OS Health Alert"}, send)

    assert status == 200
    assert calls == [(-100, 23)]


async def test_a_webhook_post_to_an_explicit_chat_is_left_as_addressed(monkeypatch):
    calls = []

    async def send(chat_id, text, parse_mode=None, topic_id=None):
        calls.append((chat_id, topic_id))

    monkeypatch.setenv("TOPIC_GENERAL", "23")

    status, _ = await _post_webhook(monkeypatch, {"text": "reminder", "chat_id": 12345}, send)

    assert status == 200
    assert calls == [(12345, None)]


async def test_a_refused_webhook_delivery_is_reported_and_answered_with_the_reason(monkeypatch):
    reports = []
    monkeypatch.setattr(
        notify, "report_undelivered", lambda where, reason, text: reports.append((where, reason)) or True
    )
    monkeypatch.delenv("TOPIC_GENERAL", raising=False)
    monkeypatch.setattr(settings, "telegram_general_topic_id", 0)

    async def refuse(chat_id, text, parse_mode=None, topic_id=None):
        raise _telethon_error(400, "TOPIC_CLOSED")

    status, body = await _post_webhook(monkeypatch, {"text": "Kronos Agent OS Health Alert"}, refuse)

    assert status == 502
    assert body["reason"] == "TOPIC_CLOSED"
    assert reports == [("chat -100 (General)", "TOPIC_CLOSED")]
