"""F09: a delivery that needs review is repaired by an operator, never faked as delivered."""

from unittest.mock import AsyncMock

import pytest

from kronos import plans
from kronos.config import settings
from kronos.delivery import drain, enqueue, resolve_review, review_queue, stream_status
from kronos.turn_ownership import TurnBusyError, own_conversation


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


def _queue(db, key="event:1", text="private result text", stream="plan:1"):
    db.write_tx(lambda conn: enqueue(conn, event_key=key, stream_key=stream, chat_id=77, topic_id=42, text=text, now=1))


def _row(db, key="event:1"):
    return dict(db.read_one("SELECT * FROM delivery_outbox WHERE event_key = ?", (key,)))


async def _stuck_on_changed_sender(db, key="event:1"):
    """First account sent nothing durable, then a different account came up."""
    _queue(db, key=key)
    db.write("UPDATE delivery_outbox SET sender_id = 55 WHERE event_key = ?", (key,))
    send = AsyncMock(return_value=101)
    assert await drain(db, sender_id=66, send=send) == 0
    send.assert_not_awaited()
    assert _row(db, key)["state"] == "needs_review"


async def test_review_queue_shows_metadata_without_text_or_recipient(db):
    await _stuck_on_changed_sender(db)
    listing = review_queue(db)
    assert [(r["event_key"], r["last_error"], r["sent_chunks"], r["total_chunks"]) for r in listing] == [
        ("event:1", "DeliveryUncertainError", 0, 1)
    ]
    flat = repr(listing)
    assert "private result text" not in flat and "77" not in flat


async def test_plain_retry_keeps_identity_and_still_refuses_another_account(db):
    await _stuck_on_changed_sender(db)
    identity = _row(db)["random_ids"]

    assert await resolve_review(db, "event:1", action="retry") is True
    assert _row(db)["state"] == "pending" and _row(db)["random_ids"] == identity

    send = AsyncMock(return_value=101)
    await drain(db, sender_id=66, send=send)
    send.assert_not_awaited()
    assert _row(db)["state"] == "needs_review", "a retry alone must not hand the message to a different account"


async def test_retry_with_rebind_delivers_through_the_current_account(db):
    await _stuck_on_changed_sender(db)
    identity = _row(db)["random_ids"]

    assert await resolve_review(db, "event:1", action="retry", rebind_sender=True) is True
    send = AsyncMock(return_value=101)
    assert await drain(db, sender_id=66, send=send) == 1

    delivered = _row(db)
    assert delivered["state"] == "delivered" and delivered["sender_id"] == 66
    assert delivered["random_ids"] == identity and delivered["receipts"] == "[101]"
    assert send.await_args.args[0].random_id == int(identity.strip("[]"))


async def test_abandon_unblocks_the_stream_without_claiming_delivery(db):
    await _stuck_on_changed_sender(db)
    _queue(db, key="event:2", text="next")
    send = AsyncMock(return_value=202)
    assert await drain(db, sender_id=66, send=send) == 0, "the stream is blocked behind the stuck delivery"

    assert await resolve_review(db, "event:1", action="abandon") is True

    abandoned = _row(db)
    assert abandoned["obsolete"] == 1 and abandoned["state"] == "needs_review" and abandoned["receipts"] == "[]"
    assert abandoned["last_error"] == "operator_abandoned"
    assert review_queue(db) == []
    assert await drain(db, sender_id=66, send=send) == 1
    assert stream_status(db, "plan:1") == {"pending": 0, "delivered": 1, "needs_review": 0}


async def test_only_deliveries_needing_review_can_be_resolved(db):
    _queue(db)
    assert await resolve_review(db, "event:1", action="abandon") is False
    assert await resolve_review(db, "event:1", action="retry") is False
    assert await resolve_review(db, "missing", action="retry") is False
    assert _row(db)["state"] == "pending" and _row(db)["obsolete"] == 0

    assert await drain(db, sender_id=66, send=AsyncMock(return_value=7)) == 1
    assert await resolve_review(db, "event:1", action="abandon") is False
    assert _row(db)["state"] == "delivered"

    with pytest.raises(ValueError):
        await resolve_review(db, "event:1", action="delivered")


async def test_a_live_sender_is_not_overruled(db):
    await _stuck_on_changed_sender(db)
    async with own_conversation(str(db.path), "delivery:event:1"):
        with pytest.raises(TurnBusyError):
            await resolve_review(db, "event:1", action="abandon")
    assert _row(db)["obsolete"] == 0


async def test_cli_lists_and_resolves_without_printing_the_message(db, monkeypatch, capsys):
    import asyncio

    import kronos.cli as cli

    await _stuck_on_changed_sender(db)
    monkeypatch.setattr(cli, "_delivery_queues", lambda: {"plans": (db, False)})

    def run(*argv):
        return asyncio.get_running_loop().run_in_executor(None, cli.main, list(argv))

    assert await run("delivery", "review") == 0
    listing = capsys.readouterr().out
    assert "event:1" in listing and "private result text" not in listing

    assert await run("delivery", "resolve", "plans", "nope", "--action", "retry") == 1
    assert await run("delivery", "resolve", "plans", "event:1", "--action", "abandon") == 0
    assert "not reported as delivered" in capsys.readouterr().out
    assert await run("delivery", "review") == 0
    assert "No deliveries need review" in capsys.readouterr().out
