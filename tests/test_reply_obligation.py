"""F09: an ordinary answer that never reached Telegram is recovered after a restart."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from langchain_core.messages import AIMessage

import kronos.bridge as bridge
from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.migrations.v011_reply_state import migrate
from kronos.session import SessionStore
from kronos.turn_delivery import RecoveryDestination

ROUTE = RecoveryDestination(chat_id=77, sender_id=55)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    import kronos.db as db_module
    import kronos.swarm_store as swarm_module

    db_module._instances.clear()
    swarm_module._singleton = None
    yield SessionStore(str(tmp_path / "session.db"), agent_name="kronos")
    db_module._instances.clear()
    swarm_module._singleton = None


def _agent(store, monkeypatch, responses):
    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=responses)
    model.bind_tools = lambda tools: model
    monkeypatch.setattr("kronos.graph.get_model", lambda tier: model)
    obj = object.__new__(KronosAgent)
    obj._session_store = store
    obj._memory_enabled = False
    obj._supervisor = None
    obj._tools = []
    obj._skill_store = None
    obj._system_prompt = "system"
    obj._external_tool_event_callback = None
    obj._durable_recovery_checked = True
    obj._last_pending_approval_id = None
    return obj


async def _finished(store, *, owed=True, content="the answer", thread="77", route=ROUTE):
    turn = await store.begin_turn(thread, "question", recovery_destination=route, reply_owed=owed)
    await store.finalize_turn(thread_id=thread, messages=[AIMessage(content=content)], turn_id=turn, content=content)
    return turn


def _state(store, turn):
    with sqlite3.connect(store.db_path) as db:
        return db.execute(
            "SELECT reply_state, delivery_requested FROM active_turns WHERE turn_id = ?", (turn,)
        ).fetchone()


def _queued(store):
    with sqlite3.connect(store.db_path) as db:
        return [row[0] for row in db.execute("SELECT chunks FROM delivery_outbox WHERE obsolete = 0 ORDER BY seq")]


async def test_a_confirmed_reply_is_never_sent_again(store):
    turn = await _finished(store)
    assert _state(store, turn) == ("owed", 0)

    assert await store.settle_reply(turn, "sent") is True
    assert await store.settle_reply(turn, "sent") is False

    assert await store.adopt_unacknowledged_replies() == 0
    assert _queued(store) == [] and _state(store, turn) == ("sent", 0)


async def test_an_unconfirmed_answer_is_queued_once_with_its_exact_text(store):
    turn = await _finished(store, content="the exact answer")

    assert await store.adopt_unacknowledged_replies() == 1
    assert await store.adopt_unacknowledged_replies() == 0

    queued = _queued(store)
    assert len(queued) == 1 and "the exact answer" in queued[0] and turn in queued[0]
    assert _state(store, turn) == ("adopted", 1)
    assert (await store.delivery_status(turn))["state"] == "pending"


async def test_adopted_answer_is_delivered_by_the_worker_to_the_original_chat(store, monkeypatch):
    turn = await _finished(store)
    await store.adopt_unacknowledged_replies()
    send = AsyncMock(return_value=101)
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 55)
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", send)

    await store.deliver_pending()

    assert send.await_count == 1 and send.call_args.args[0].chat_id == 77
    assert (await store.delivery_status(turn))["state"] == "delivered"


async def test_only_finished_owed_recent_turns_are_adopted(store):
    not_owed = await _finished(store, owed=False)
    waived = await _finished(store)
    await store.settle_reply(waived, "waived")
    running = await store.begin_turn("77", "still working", recovery_destination=ROUTE, reply_owed=True)
    old = await _finished(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET completed_at = datetime('now', '-3 days') WHERE turn_id = ?", (old,))

    assert await store.adopt_unacknowledged_replies() == 0
    assert _queued(store) == []
    assert [_state(store, t)[0] for t in (not_owed, waived, running, old)] == ["", "waived", "owed", "owed"]


async def test_a_failed_turn_is_reported_as_unfinished_not_as_an_answer(store):
    turn = await store.begin_turn("77", "question", recovery_destination=ROUTE, reply_owed=True)
    await store.fail_turn(turn, "model exploded")
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM delivery_outbox")

    assert await store.adopt_unacknowledged_replies() == 1
    assert "не завершено" in _queued(store)[0]


async def test_a_reply_cannot_be_owed_without_a_route(store):
    turn = await store.begin_turn("77", "question", reply_owed=True)
    assert _state(store, turn) == ("", 0)
    with pytest.raises(ValueError):
        await store.settle_reply(turn, "delivered")


async def test_a_turn_already_recovered_through_resume_is_not_queued_twice(store, monkeypatch):
    """Resume owns its own delivery; adoption must not add a second message."""
    turn = await _finished(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET delivery_requested = 1 WHERE turn_id = ?", (turn,))
    assert await store.adopt_unacknowledged_replies() == 0
    assert _queued(store) == []


async def test_bridge_marks_the_turn_owed_and_confirms_after_sending(store, monkeypatch):
    agent = _agent(store, monkeypatch, [AIMessage(content="answer")])
    monkeypatch.setattr(bridge, "_agent", agent)
    monkeypatch.setattr(bridge, "_my_id", 55)

    turns: list[str] = []
    assert await bridge._ask_agent("question", 77, 101, turn_sink=turns) == "answer"
    assert len(turns) == 1 and _state(store, turns[0]) == ("owed", 0)

    await bridge._settle_reply(turns)
    assert _state(store, turns[0]) == ("sent", 0)
    assert await store.adopt_unacknowledged_replies() == 0


async def test_a_crash_between_the_answer_and_the_send_is_recovered_on_restart(store, monkeypatch):
    agent = _agent(store, monkeypatch, [AIMessage(content="answer the user never saw")])
    monkeypatch.setattr(bridge, "_agent", agent)
    monkeypatch.setattr(bridge, "_my_id", 55)
    turns: list[str] = []
    await bridge._ask_agent("question", 77, 101, turn_sink=turns)
    # The process dies here: no send, no confirmation.

    restarted = SessionStore(store.db_path, agent_name="kronos")
    assert await restarted.adopt_unacknowledged_replies() == 1
    assert "answer the user never saw" in _queued(restarted)[0]


async def test_callers_that_do_not_take_on_the_reply_owe_nothing(store, monkeypatch):
    agent = _agent(store, monkeypatch, [AIMessage(content="a"), AIMessage(content="b")])
    monkeypatch.setattr(bridge, "_agent", agent)
    monkeypatch.setattr(bridge, "_my_id", 55)

    await bridge._ask_agent("question", 77, 101)
    turns: list[str] = []
    await bridge._ask_agent("peer said", 77, 101, source_kind="peer_reaction", persist_user_turn=False, turn_sink=turns)

    assert turns == []
    assert [row["turn_id"] for row in await store.list_turns()] and await store.adopt_unacknowledged_replies() == 0


async def test_confirmation_failure_does_not_break_the_handler(store, monkeypatch):
    broken = SimpleNamespace(session_store=SimpleNamespace(settle_reply=AsyncMock(side_effect=OSError("disk"))))
    monkeypatch.setattr(bridge, "_agent", broken)
    await bridge._settle_reply(["turn-1"])
    monkeypatch.setattr(bridge, "_agent", SimpleNamespace(session_store=None))
    await bridge._settle_reply(["turn-1"])


async def test_migration_owes_nothing_for_existing_turns(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE active_turns (turn_id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO active_turns VALUES ('old', 'done')")
    async with aiosqlite.connect(path) as db:
        await migrate(db)
        await migrate(db)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT reply_state FROM active_turns").fetchone()[0] == ""


async def test_a_turn_still_running_or_waiting_cannot_be_settled(store):
    """What it sent was a prompt, not its answer; the answer is still owed."""
    turn = await store.begin_turn("77", "question", recovery_destination=ROUTE, reply_owed=True)
    assert await store.settle_reply(turn, "sent") is False
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE active_turns SET status = 'waiting_approval' WHERE turn_id = ?", (turn,))
    assert await store.settle_reply(turn, "sent") is False
    assert _state(store, turn) == ("owed", 0)

    await store.fail_turn(turn, "stopped")
    assert await store.settle_reply(turn, "sent") is True
