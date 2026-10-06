"""F09: a cron notification Telegram verifiably did not accept is retried, not dropped."""

import io
import json
import sqlite3
import urllib.error
from pathlib import Path

import pytest

from kronos.config import settings
from kronos.cron import notify, notify_retry

URL = "https://api.telegram.org/botSECRET-TOKEN/sendMessage"
BODY = {"chat_id": -100, "text": "daily digest", "message_thread_id": 7}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tg_bot_token", "SECRET-TOKEN")
    import kronos.db as db_module

    db_module._instances.clear()
    monkeypatch.setattr(notify, "send_ntfy", lambda *args, **kwargs: None)
    monkeypatch.setattr(notify, "_undelivered_reports", {})
    yield tmp_path
    db_module._instances.clear()


def _http_error(code, description="Internal Server Error"):
    payload = json.dumps({"ok": False, "error_code": code, "description": description}).encode()
    return urllib.error.HTTPError(URL, code, description, None, io.BytesIO(payload))


def _transport(monkeypatch, *outcomes):
    """Each call pops the next outcome: an exception to raise, or 200."""
    calls = []
    remaining = list(outcomes)

    def urlopen(request, timeout=0):
        calls.append(json.loads(request.data))
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return type("Response", (), {"status": outcome})()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    return calls


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(500),
        _http_error(502),
        _http_error(429, "Too Many Requests"),
        urllib.error.URLError(ConnectionRefusedError()),
    ],
)
def test_verified_non_acceptance_is_queued(queue, monkeypatch, failure):
    _transport(monkeypatch, failure)
    assert notify._send_message(URL, dict(BODY)) is False
    assert notify_retry.pending_count() == {"pending": 1, "dead": 0}


@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError(TimeoutError()), TimeoutError(), ConnectionResetError()],
)
def test_an_unknown_outcome_is_not_repeated_automatically(queue, monkeypatch, failure):
    """The request may already be on Telegram's side: a retry could duplicate it."""
    _transport(monkeypatch, failure)
    assert notify._send_message(URL, dict(BODY)) is False
    assert notify_retry.pending_count() == {"pending": 0, "dead": 0}


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(400, "Bad Request: TOPIC_CLOSED"),
        _http_error(403, "Forbidden: bot was kicked"),
        _http_error(404, "Not Found"),
    ],
)
def test_a_final_refusal_is_not_queued(queue, monkeypatch, failure):
    _transport(monkeypatch, failure)
    assert notify._send_message(URL, dict(BODY)) is False
    assert notify_retry.pending_count() == {"pending": 0, "dead": 0}


def test_a_successful_send_queues_nothing(queue, monkeypatch):
    _transport(monkeypatch, 200)
    assert notify._send_message(URL, dict(BODY)) is True
    assert notify_retry.pending_count() == {"pending": 0, "dead": 0}


def test_the_queue_never_stores_the_bot_token(queue, monkeypatch):
    _transport(monkeypatch, _http_error(500))
    notify._send_message(URL, dict(BODY))
    with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
        stored = repr(db.execute("SELECT * FROM notify_retry").fetchall())
    assert "SECRET-TOKEN" not in stored and "daily digest" in stored


def test_a_queued_notification_is_delivered_once_when_telegram_recovers(queue, monkeypatch):
    calls = _transport(monkeypatch, _http_error(500), 200)
    notify._send_message(URL, dict(BODY))

    assert notify_retry.retry_due(now=0) == 0, "not due yet"
    assert notify_retry.retry_due() == 0, "still inside its backoff"
    assert len(calls) == 1

    with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
        db.execute("UPDATE notify_retry SET next_attempt = 0")
    assert notify_retry.retry_due() == 1
    assert calls[1] == BODY
    assert notify_retry.pending_count() == {"pending": 0, "dead": 0}
    assert notify_retry.retry_due() == 0 and len(calls) == 2


def test_retries_back_off_and_stop_with_a_visible_report(queue, monkeypatch):
    reports = []
    monkeypatch.setattr(notify, "report_undelivered", lambda *args, **kwargs: reports.append(args) or True)
    _transport(monkeypatch, *[_http_error(503) for _ in range(notify_retry.MAX_ATTEMPTS + 1)])
    notify._send_message(URL, dict(BODY))

    now, gaps = 1e12, []
    for _ in range(notify_retry.MAX_ATTEMPTS):
        with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
            before = db.execute("SELECT next_attempt FROM notify_retry").fetchone()[0]
            db.execute("UPDATE notify_retry SET next_attempt = 0, created_at = ?", (now,))
        notify_retry.retry_due(now=now)
        with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
            state, next_attempt = db.execute("SELECT state, next_attempt FROM notify_retry").fetchone()
        gaps.append(next_attempt - now if state == "pending" else None)
        del before

    assert gaps[:-1] == sorted(gaps[:-1]) and gaps[0] > 0, "each retry waits longer"
    assert gaps[-1] is None
    assert notify_retry.pending_count() == {"pending": 0, "dead": 1}
    assert len(reports) == 1 and "retry budget exhausted" in reports[0][1]
    assert notify_retry.retry_due(now=now * 2) == 0, "a dead notification is never sent again"


def test_a_stale_notification_is_not_delivered_a_day_late(queue, monkeypatch):
    calls = _transport(monkeypatch, _http_error(500), _http_error(500))
    notify._send_message(URL, dict(BODY))
    with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
        db.execute("UPDATE notify_retry SET next_attempt = 0, created_at = 0")
    notify_retry.retry_due(now=notify_retry.MAX_AGE_SECONDS + 1)
    assert notify_retry.pending_count() == {"pending": 0, "dead": 1} and len(calls) == 2


def test_a_refusal_or_unknown_outcome_on_retry_ends_the_retries(queue, monkeypatch):
    _transport(monkeypatch, _http_error(500), _http_error(400, "Bad Request: TOPIC_CLOSED"))
    notify._send_message(URL, dict(BODY))
    with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
        db.execute("UPDATE notify_retry SET next_attempt = 0")
    assert notify_retry.retry_due() == 0
    assert notify_retry.pending_count() == {"pending": 0, "dead": 1}


def test_a_long_message_keeps_its_tail_in_order_behind_the_queued_chunk(queue, monkeypatch):
    calls = _transport(monkeypatch, 200, _http_error(500))
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(130))
    assert len(notify._split_by_lines(text)) >= 3

    assert notify.send_bot_api(text, chat_id=-100, parse_mode="") is False
    assert len(calls) == 2, "sending stops at the first chunk Telegram did not accept"

    with sqlite3.connect(Path(queue, "notify_retry.db")) as db:
        queued = [json.loads(row[0])["text"] for row in db.execute("SELECT body FROM notify_retry ORDER BY id")]
    assert queued == notify._split_by_lines(text)[1:]


def test_queue_failure_never_breaks_the_sender(queue, monkeypatch):
    _transport(monkeypatch, _http_error(500))
    monkeypatch.setattr(notify_retry, "_db", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    assert notify._send_message(URL, dict(BODY)) is False


def test_cron_registers_the_retry_job():
    from kronos.cron.scheduler import Scheduler
    from kronos.cron.setup import setup_cron_jobs

    scheduler = Scheduler()
    setup_cron_jobs(scheduler)
    assert scheduler.jobs["notify-retry"].interval_seconds == 300
