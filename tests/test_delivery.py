"""The producer's commit, not an in-memory callback, owns delivery."""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from kronos import plans
from kronos.config import settings
from kronos.delivery import DeliveryRetryError, DeliveryUncertainError, drain, enqueue, split_text
from kronos.effect_state import DurableStateError
from kronos.migrations.v007_delivery_outbox import migrate
from kronos.turn_ownership import own_conversation


@pytest.fixture
def db(tmp_path, monkeypatch):
    import kronos.db as module

    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 0)
    module._instances.clear()
    store = plans._db()
    yield store
    module._instances.clear()


def queue(db, key="event:1", text="result", stream="plan:1"):
    db.write_tx(lambda conn: enqueue(conn, event_key=key, stream_key=stream, chat_id=77, topic_id=42, text=text, now=1))


def row(db, key="event:1"):
    return dict(db.read_one("SELECT * FROM delivery_outbox WHERE event_key = ?", (key,)))


def done_plan():
    p = plans.create_plan(agent_name="kronos", goal="reliable result", chat_id=77)
    s = plans.add_step(p, "work")
    plans.finish_step(s, "done")
    plans.settle_plan(p)
    return p


def test_migration_is_idempotent_and_does_not_replay_old_summaries(db):
    p = done_plan()
    db.write("UPDATE plans SET summary = 'already generated' WHERE id = ?", (p,))
    db.init_schema(migrate)
    db.init_schema(migrate)
    assert plans.delivery_status(plans.get_plan(p))["summary"] == "legacy_unknown"
    assert db.read("SELECT * FROM delivery_outbox") == []


def test_enqueue_requires_transaction_and_rejects_payload_drift(db):
    with pytest.raises(DurableStateError):
        enqueue(db.conn, event_key="k", stream_key="s", chat_id=77, topic_id=None, text="text")
    queue(db)
    before = row(db)
    queue(db)
    assert row(db) == before
    with pytest.raises(DurableStateError):
        queue(db, text="different")
    assert row(db) == before


def test_summary_and_outbox_commit_together_and_freeze_first_result(db, monkeypatch):
    p = done_plan()
    original = plans.enqueue

    def fail_after_enqueue(conn, **kwargs):
        original(conn, **kwargs)
        raise OSError("simulated disk error")

    monkeypatch.setattr(plans, "enqueue", fail_after_enqueue)
    with pytest.raises(OSError):
        plans.set_summary(p, "summary")
    assert plans.get_plan(p)["summary"] == ""
    assert db.read("SELECT * FROM delivery_outbox") == []
    monkeypatch.setattr(plans, "enqueue", original)
    assert plans.set_summary(p, "summary")
    assert not plans.set_summary(p, "new model answer")
    assert plans.get_plan(p)["summary"] == "summary"
    assert plans.plans_awaiting_summary("kronos") == []
    assert plans.delivery_status(plans.get_plan(p))["summary"] == "pending"


async def test_progress_commit_cannot_lose_its_notification(db, monkeypatch):
    p = plans.create_plan(agent_name="kronos", goal="work", chat_id=77)
    s = plans.add_step(p, "step", notify=True)
    async with own_conversation(settings.db_path, f"plan:{p}") as owner:
        assert plans.claim_step(s, ownership=owner)
        plans.link_turn(s, "turn1", execution_key=plans.get_step(s)["execution_key"])
        original = plans.enqueue
        monkeypatch.setattr(plans, "enqueue", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            plans.complete_step_turn(s, "turn1", "finished")
        assert plans.get_step(s)["state"] == "running"
        monkeypatch.setattr(plans, "enqueue", original)
        assert plans.complete_step_turn(s, "turn1", "finished")
        assert not plans.complete_step_turn(s, "turn1", "duplicate")
    assert len(db.read("SELECT * FROM delivery_outbox")) == 1
    assert plans.get_step(s)["state"] == "done"


async def test_no_transport_readiness_does_not_consume_attempt(db):
    queue(db)
    send = AsyncMock(return_value=12)
    assert await drain(db, sender_id=0, send=send) == 0
    send.assert_not_awaited()
    assert row(db)["attempts"] == row(db)["sender_id"] == 0
    assert await drain(db, sender_id=55, send=send) == 1
    assert row(db)["state"] == "delivered"


@pytest.mark.parametrize("result", [None, False, 0, -1, "message-id"])
async def test_missing_receipt_is_pending_not_success(db, result):
    queue(db)
    send = AsyncMock(return_value=result)
    assert await drain(db, sender_id=55, send=send, now=lambda: 10) == 0
    assert row(db)["state"] == "pending"
    assert row(db)["next_attempt"] > 10
    await drain(db, sender_id=55, send=send, now=lambda: 11)
    send.assert_awaited_once()


async def test_partial_send_restarts_at_first_unacknowledged_chunk(db):
    queue(db, text="🙂" * 4000)
    calls = []

    async def send(chunk):
        calls.append(chunk)
        if len(calls) == 2:
            raise DeliveryRetryError(600)
        return len(calls) + 10

    await drain(db, sender_id=55, send=send, now=lambda: 10)
    saved = row(db)
    assert saved["next_chunk"] == 1 and saved["next_attempt"] == 610
    await drain(db, sender_id=55, send=send, now=lambda: 611)
    assert row(db)["state"] == "delivered"
    assert calls[1] == calls[2]
    assert calls[0].random_id != calls[2].random_id
    assert all(len(c.text.encode("utf-16-le")) // 2 <= 3500 for c in calls)


@pytest.mark.parametrize("committed", [False, True])
async def test_lost_acknowledgement_never_regenerates_id_or_regresses_receipt(db, monkeypatch, committed):
    queue(db)
    requests = []
    accepted = {}

    async def provider(chunk):
        requests.append(chunk.random_id)
        return accepted.setdefault(chunk.random_id, 23)

    original = db.write
    failed = False

    def broken(sql, args=()):
        nonlocal failed
        if "SET next_chunk" in sql and not failed:
            failed = True
            if committed:
                original(sql, args)
            raise OSError("ack lost")
        return original(sql, args)

    monkeypatch.setattr(db, "write", broken)
    await drain(db, sender_id=55, send=provider, now=lambda: 10)
    await drain(db, sender_id=55, send=provider, now=lambda: 100)
    assert row(db)["state"] == "delivered"
    assert len(set(requests)) == len(accepted) == 1
    assert len(requests) == (1 if committed else 2)


async def test_two_drainers_cannot_send_the_same_chunk(db):
    queue(db)
    started, release = asyncio.Event(), asyncio.Event()

    async def sender(chunk):
        started.set()
        await release.wait()
        return 12

    first = asyncio.create_task(drain(db, sender_id=55, send=sender))
    await started.wait()
    other = AsyncMock(return_value=13)
    assert await drain(db, sender_id=55, send=other) == 0
    other.assert_not_awaited()
    release.set()
    assert await first == 1


async def test_cancel_preserves_identity_until_a_new_worker_owns_delivery(db):
    queue(db)
    started = asyncio.Event()
    requests = []

    async def interrupted(chunk):
        requests.append(chunk)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(drain(db, sender_id=55, send=interrupted))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    send = AsyncMock(return_value=12)
    await drain(db, sender_id=55, send=send)
    assert send.call_args.args[0] == requests[0]
    assert row(db)["state"] == "delivered"


async def test_sender_change_requires_review_and_does_not_dispatch(db):
    queue(db)
    await drain(db, sender_id=55, send=AsyncMock(side_effect=TimeoutError()), now=lambda: 10)
    send = AsyncMock(return_value=12)
    await drain(db, sender_id=66, send=send, now=lambda: 100)
    assert row(db)["state"] == "needs_review"
    assert row(db)["sender_id"] == 55
    send.assert_not_awaited()


async def test_ambiguous_error_is_visible_and_never_replaced_by_new_send(db, caplog):
    queue(db)
    send = AsyncMock(side_effect=DeliveryUncertainError("private-secret"))
    await drain(db, sender_id=55, send=send)
    await drain(db, sender_id=55, send=send)
    send.assert_awaited_once()
    assert row(db)["state"] == "needs_review"
    assert "private-secret" not in caplog.text
    assert "private-secret" not in row(db)["last_error"]


async def test_stream_order_and_failed_stream_do_not_block_another_plan(db):
    queue(db, key="progress")
    queue(db, key="summary")
    queue(db, key="other", stream="plan:2")
    calls = []

    async def send(chunk):
        calls.append(chunk.random_id)
        if chunk.random_id == json.loads(row(db, "progress")["random_ids"])[0]:
            raise TimeoutError()
        return 123

    await drain(db, sender_id=55, send=send, now=lambda: 10)
    assert row(db, "summary")["attempts"] == 0
    assert row(db, "other")["state"] == "delivered"


def test_invalid_unicode_and_destination_roll_back(db):
    for kwargs in [{"text": "\ud800"}, {"chat_id": 0}, {"topic_id": -1}, {"text": "  "}]:
        params = dict(event_key="e", stream_key="s", chat_id=77, topic_id=None, text="ok") | kwargs
        with pytest.raises(ValueError):
            db.write_tx(lambda conn: enqueue(conn, **params))
    assert db.read("SELECT * FROM delivery_outbox") == []
    assert "".join(split_text("a🙂" * 2500)) == "a🙂" * 2500


async def test_corrupt_checkpoint_requires_review_without_sending(db):
    queue(db)
    db.write("UPDATE delivery_outbox SET next_chunk = 2")
    send = AsyncMock(return_value=12)
    await drain(db, sender_id=55, send=send)
    assert row(db)["state"] == "needs_review"
    send.assert_not_awaited()


async def test_cancelled_plan_notifies_only_after_safe_cleanup_and_without_model(db, monkeypatch):
    from kronos.cron import plans as poller
    from kronos.session import SessionStore

    class Agent:
        session_store = SessionStore(settings.db_path)
        ainvoke = AsyncMock(side_effect=AssertionError("no summary model on stop"))

    monkeypatch.setattr("kronos.bridge.get_agent", lambda: Agent())
    p = plans.create_plan(agent_name="kronos", goal="stop test", chat_id=77)
    plans.add_step(p, "work")
    assert plans.cancel_plan(p, "kronos")
    await poller._deliver_pending_summaries(2)
    assert not plans.get_plan(p)["summary"]
    await poller._reconcile_stops()
    await poller._deliver_pending_summaries(2)
    assert "остановлен" in plans.get_plan(p)["summary"]
    assert "не завершено" in plans.get_plan(p)["summary"]
    assert plans.delivery_status(plans.get_plan(p))["summary"] == "pending"
    Agent.ainvoke.assert_not_awaited()


def test_historical_cancel_does_not_trigger_upgrade_spam(db):
    p = plans.create_plan(agent_name="kronos", goal="legacy cancelled", chat_id=77)
    db.write("UPDATE plans SET state = 'cancelled', stop_reason = 'plan_cancelled' WHERE id = ?", (p,))
    assert plans.plans_awaiting_summary("kronos") == []


def test_delivery_schema_survives_reopen(db):
    queue(db)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT state FROM delivery_outbox").fetchone()[0] == "pending"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


async def test_stalled_transport_times_out_without_losing_or_acknowledging_work(db, monkeypatch):
    monkeypatch.setattr("kronos.delivery.SEND_TIMEOUT_SECONDS", 0.01)
    queue(db)

    async def stuck(chunk):
        await asyncio.Event().wait()

    await drain(db, sender_id=55, send=stuck, now=lambda: 10)
    assert row(db)["state"] == "pending"
    assert row(db)["next_chunk"] == 0
    assert row(db)["last_error"] == "TimeoutError"


async def test_worker_survives_db_error_and_waits_for_transport_readiness(db, monkeypatch):
    from types import SimpleNamespace

    from kronos.cron import delivery as worker

    queue(db)
    available = False
    calls = sleeps = 0
    original = plans.deliver_pending
    send = AsyncMock(return_value=12)
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 55 if available else 0)
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", send)

    async def deliver():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary DB failure")
        return await original()

    async def sleep(seconds):
        nonlocal sleeps, available
        sleeps += 1
        if sleeps == 2:
            assert row(db)["attempts"] == 0
            available = True
        if sleeps == 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(plans, "deliver_pending", deliver)
    monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await worker.run_delivery_worker()
    assert row(db)["state"] == "delivered"
    send.assert_awaited_once()


async def test_plan_execution_never_waits_for_a_stalled_delivery_service(db, monkeypatch):
    from kronos.cron import plans as poller

    p = done_plan()
    monkeypatch.setattr("kronos.bridge.get_agent", lambda: None)
    transport = AsyncMock(side_effect=AssertionError("plan execution must not dispatch"))
    monkeypatch.setattr(plans, "deliver_pending", transport)
    await poller.run_due_plan_steps()
    assert plans.get_plan(p)["summary"]
    assert plans.delivery_status(plans.get_plan(p))["summary"] == "pending"
    transport.assert_not_awaited()
