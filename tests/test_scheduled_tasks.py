"""Durable scheduled tasks / reminders (roadmap 4.2)."""

import time
from datetime import UTC, datetime, timedelta

import pytest

import kronos.db as _db
from kronos import scheduled_tasks
from kronos.audit import reset_tool_audit_context, set_tool_audit_context
from kronos.config import settings


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    _db._instances.clear()
    yield
    _db._instances.clear()


def _add(run_at, message="ping", recur=0, agent="kronos", chat=42):
    return scheduled_tasks.add_task(
        agent_name=agent,
        chat_id=chat,
        topic_id=None,
        thread_id=str(chat),
        run_at=run_at,
        message=message,
        recur_seconds=recur,
    )


def test_add_and_due_filters_future(isolated_db):
    now = time.time()
    _add(now - 10, "past-due")
    _add(now + 3600, "future")
    due = scheduled_tasks.due_tasks("kronos", now=now)
    assert [t["message"] for t in due] == ["past-due"]


def test_complete_oneshot_marks_done(isolated_db):
    now = time.time()
    tid = _add(now - 1)
    scheduled_tasks.complete_task(tid, 0, now - 1)
    assert scheduled_tasks.due_tasks("kronos", now=now) == []
    assert scheduled_tasks.list_pending("kronos") == []


def test_complete_recurring_bumps_run_at(isolated_db):
    now = time.time()
    tid = _add(now - 1, "daily", recur=86400)
    scheduled_tasks.complete_task(tid, 86400, now - 1)
    pending = scheduled_tasks.list_pending("kronos")
    assert len(pending) == 1
    assert pending[0]["id"] == tid
    assert pending[0]["run_at"] > now  # rescheduled, not done


def test_cancel_is_idempotent(isolated_db):
    tid = _add(time.time() + 100)
    assert scheduled_tasks.cancel_task(tid, "kronos") is True
    assert scheduled_tasks.cancel_task(tid, "kronos") is False  # already cancelled
    assert scheduled_tasks.list_pending("kronos") == []


def test_due_is_scoped_per_agent(isolated_db):
    now = time.time()
    _add(now - 1, "kronos-task", agent="kronos")
    assert scheduled_tasks.due_tasks("nexus", now=now) == []


def test_schedule_task_tool_adds_with_chat_and_topic(isolated_db):
    from kronos.tools.reminders import schedule_task

    token = set_tool_audit_context(agent="kronos", thread_id="42:7", session_id="42")
    try:
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        result = schedule_task.invoke({"when_iso": future, "message": "buy milk"})
    finally:
        reset_tool_audit_context(token)

    assert "Запланировал" in result
    pending = scheduled_tasks.list_pending("kronos")
    assert len(pending) == 1
    assert pending[0]["chat_id"] == 42
    assert pending[0]["topic_id"] == 7  # parsed from thread_id "42:7"
    assert pending[0]["message"] == "buy milk"


def test_schedule_task_tool_rejects_past_time(isolated_db):
    from kronos.tools.reminders import schedule_task

    token = set_tool_audit_context(agent="kronos", thread_id="42", session_id="42")
    try:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        result = schedule_task.invoke({"when_iso": past, "message": "x"})
    finally:
        reset_tool_audit_context(token)

    assert "прошл" in result.lower()
    assert scheduled_tasks.list_pending("kronos") == []


def test_schedule_task_tool_without_chat_context(isolated_db):
    from kronos.tools.reminders import schedule_task

    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    result = schedule_task.invoke({"when_iso": future, "message": "x"})  # no audit context
    assert "неизвестен чат" in result.lower()


async def test_run_due_reminders_fires_and_completes(isolated_db, monkeypatch):
    from kronos.cron import reminders as cron_reminders

    now = time.time()
    _add(now - 1, "ring", chat=99)
    sent = []

    def fake_send(text, chat_id=None, parse_mode=None, topic_id=None):
        sent.append((text, chat_id))
        return True

    monkeypatch.setattr(cron_reminders, "send_webhook", fake_send)
    await cron_reminders.run_due_reminders()

    assert ("ring", 99) in sent
    assert scheduled_tasks.list_pending("kronos") == []  # one-shot completed


async def test_run_due_reminders_keeps_pending_on_failure(isolated_db, monkeypatch):
    from kronos.cron import reminders as cron_reminders

    now = time.time()
    _add(now - 1, "ring")
    monkeypatch.setattr(cron_reminders, "send_webhook", lambda *a, **k: False)
    await cron_reminders.run_due_reminders()

    # delivery failed → still pending for retry
    assert len(scheduled_tasks.list_pending("kronos")) == 1


def test_schedule_followup_tool_adds_followup_kind(isolated_db):
    from kronos.tools.reminders import schedule_followup

    token = set_tool_audit_context(agent="kronos", thread_id="42", session_id="42")
    try:
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        result = schedule_followup.invoke({"when_iso": future, "task": "check DEV-1698"})
    finally:
        reset_tool_audit_context(token)

    assert "Вернусь" in result
    pending = scheduled_tasks.list_pending("kronos")
    assert len(pending) == 1
    assert pending[0]["kind"] == "followup"
    assert pending[0]["message"] == "check DEV-1698"


async def test_run_due_followup_invokes_agent_and_sends_result(isolated_db, monkeypatch):
    from kronos.cron import reminders as cron_reminders

    now = time.time()
    scheduled_tasks.add_task(
        agent_name="kronos",
        chat_id=7,
        topic_id=None,
        thread_id="7",
        run_at=now - 1,
        message="do the thing",
        recur_seconds=0,
        kind="followup",
    )

    class FakeAgent:
        async def ainvoke(self, message, **kwargs):
            return f"result for: {message[:12]}"

    monkeypatch.setattr("kronos.bridge._agent", FakeAgent())
    sent: list[str] = []
    monkeypatch.setattr(
        cron_reminders,
        "send_webhook",
        lambda text, *a, **k: sent.append(text) or True,
    )

    await cron_reminders.run_due_reminders()

    assert sent and sent[0].startswith("result for:")
    assert scheduled_tasks.list_pending("kronos") == []  # completed


async def test_run_due_followup_retries_when_agent_not_ready(isolated_db, monkeypatch):
    from kronos.cron import reminders as cron_reminders

    now = time.time()
    scheduled_tasks.add_task(
        agent_name="kronos",
        chat_id=7,
        topic_id=None,
        thread_id="7",
        run_at=now - 1,
        message="x",
        recur_seconds=0,
        kind="followup",
    )
    monkeypatch.setattr("kronos.bridge._agent", None)
    sent: list[int] = []
    monkeypatch.setattr(cron_reminders, "send_webhook", lambda *a, **k: sent.append(1) or True)

    await cron_reminders.run_due_reminders()

    assert sent == []  # nothing delivered
    assert len(scheduled_tasks.list_pending("kronos")) == 1  # still pending


def test_kind_column_migration_on_legacy_db(isolated_db):
    import kronos.db as db_mod

    db = db_mod.get_db("scheduled_tasks")
    # Simulate a DB created before the kind column existed.
    db.write("DROP TABLE IF EXISTS scheduled_tasks")
    db.write(
        "CREATE TABLE scheduled_tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT NOT NULL, "
        "chat_id INTEGER NOT NULL, topic_id INTEGER, thread_id TEXT NOT NULL, "
        "run_at REAL NOT NULL, recur_seconds INTEGER NOT NULL DEFAULT 0, "
        "message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
        "created_at REAL NOT NULL)"
    )
    db.init_schema(scheduled_tasks._init_schema)
    cols = {row[1] for row in db.read("PRAGMA table_info(scheduled_tasks)")}
    assert "kind" in cols
