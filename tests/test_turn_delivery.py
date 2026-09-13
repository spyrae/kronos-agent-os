"""Recovery owns its notification before execution, not after finalization."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from kronos.config import settings
from kronos.effect_state import DurableStateError
from kronos.session import SessionStore
from kronos.turn_delivery import RecoveryDestination
from kronos.turn_ownership import own_conversation
from tests.test_durable_resume import ScriptedModel, Sender

THREAD = "77:42"
ROUTE = RecoveryDestination(77, 55, topic_id=42)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "swarm_db_path", str(tmp_path / "swarm.db"))
    monkeypatch.setattr(settings, "tool_approvals_enabled", False)
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 0)
    import kronos.db as databases
    import kronos.swarm_store as swarm

    databases._instances.clear()
    swarm._singleton = None
    yield SessionStore(settings.db_path, agent_name="kronos")
    databases._instances.clear()
    swarm._singleton = None


def query(store, sql, params=()):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, params)]


def queue(store):
    return query(store, "SELECT * FROM delivery_outbox ORDER BY seq")


def execute(store, sql, params=()):
    with sqlite3.connect(store.db_path) as db:
        db.execute(sql, params)


async def interrupted(store, destination=ROUTE, *, claim=True):
    turn = await store.begin_turn(THREAD, "question", recovery_destination=destination)
    await store.append_turn_messages(turn_id=turn, thread_id=THREAD, messages=[HumanMessage(content="question")])
    if claim:
        async with own_conversation(store.db_path, THREAD) as owner:
            await store.claim_turn_for_resume(turn, ownership=owner, notify=True)
    return turn


async def finish(store, turn, content="answer"):
    await store.finalize_turn(thread_id=THREAD, turn_id=turn, messages=[AIMessage(content=content)], content=content)


def agent(store, monkeypatch, responses, tools=None):
    from kronos import graph

    model = ScriptedModel(responses)
    monkeypatch.setattr(graph, "get_model", lambda *args: model)
    monkeypatch.setattr(graph.KronosAgent, "_init_tools", lambda self: None)
    value = graph.KronosAgent(tools=tools or [], session_store=store, enable_memory=False, enable_supervisor=False)
    value._system_prompt = "system"
    return value, model


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chat_id": 0},
        {"chat_id": True},
        {"sender_id": 0},
        {"sender_id": True},
        {"topic_id": -1},
        {"topic_id": True},
        {"review_required": "yes"},
    ],
)
def test_route_validation(kwargs):
    with pytest.raises(ValueError):
        RecoveryDestination(**({"chat_id": 77, "sender_id": 55} | kwargs))


async def test_route_cannot_be_attached_to_another_thread(store):
    with pytest.raises(DurableStateError):
        await store.begin_turn("another", "text", recovery_destination=ROUTE)
    assert await store.list_turns() == []


async def test_original_telegram_turn_records_provenance_but_does_not_double_send(store, monkeypatch):
    value, _ = agent(store, monkeypatch, [AIMessage(content="original answer")])
    result = await value.ainvoke_outcome("question", THREAD, recovery_destination=ROUTE)
    assert result.status == "completed"
    assert await store.recovery_destination(result.turn_id) == ROUTE
    assert (await store.delivery_status(result.turn_id))["state"] == "not_requested"
    assert queue(store) == []


async def test_resume_records_delivery_duty_before_model_execution(store, monkeypatch):
    turn = await interrupted(store, claim=False)
    value, model = agent(store, monkeypatch, [AIMessage(content="answer")])
    original = model.ainvoke

    async def checked(*args, **kwargs):
        assert (await store.delivery_status(turn))["requested"] is True
        assert await store.recovery_destination(turn) == ROUTE
        return await original(*args, **kwargs)

    monkeypatch.setattr(model, "ainvoke", checked)
    assert await value.resume_abandoned_turns() == 1
    assert queue(store)[0]["sender_id"] == 55
    assert (await store.delivery_status(turn))["state"] == "pending"


async def test_history_result_and_outbox_rollback_together(store, monkeypatch):
    import kronos.session as session

    turn = await interrupted(store)
    real = session.queue_result

    async def broken(*args, **kwargs):
        await real(*args, **kwargs)
        raise OSError("injected producer failure")

    with monkeypatch.context() as patch:
        patch.setattr(session, "queue_result", broken)
        with pytest.raises(OSError):
            await finish(store, turn)
    assert (await store.get_turn_detail(turn))["status"] == "resuming"
    assert await store.load(THREAD) == []
    assert queue(store) == []
    assert (await store.get_turn_detail(turn))["journal"]
    await finish(store, turn)
    assert (await store.get_turn_outcome(turn)).status == "completed"
    assert len(queue(store)) == 1
    before = queue(store)
    await finish(store, turn)
    await store.fail_turn(turn, "late observer error")
    await store.finish_turn(turn)
    assert queue(store) == before
    assert (await store.get_turn_outcome(turn)).content == "answer"
    with pytest.raises(DurableStateError):
        await finish(store, turn, "different")


@pytest.mark.parametrize("corrupt", [False, True])
async def test_legacy_numeric_thread_never_guesses_a_recipient(store, monkeypatch, corrupt):
    turn = await interrupted(store, destination=None)
    if corrupt:
        execute(store, "UPDATE active_turns SET recovery_destination = '{}' WHERE turn_id = ?", (turn,))
    await finish(store, turn)
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 55)
    send = AsyncMock(return_value=1)
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", send)
    await store.deliver_pending()
    assert (await store.delivery_status(turn))["state"] == ("destination_invalid" if corrupt else "destination_missing")
    assert (await store.get_turn_outcome(turn)).content == "answer"
    send.assert_not_awaited()


async def test_completed_queue_recovers_without_model_or_webhook(store, monkeypatch):
    turn = await interrupted(store)
    await finish(store, turn)
    before = queue(store)[0]
    assert await store.deliver_pending() == 0
    assert queue(store)[0] == before
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 55)
    send = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", send)
    restarted = SessionStore(store.db_path)
    assert await restarted.deliver_pending() == 0
    assert (await restarted.delivery_status(turn))["state"] == "pending"
    execute(store, "UPDATE delivery_outbox SET next_attempt = 0")
    send.side_effect = None
    send.return_value = 99
    assert await SessionStore(store.db_path).deliver_pending() == 1
    assert (await restarted.delivery_status(turn))["state"] == "delivered"
    assert send.call_args_list[0].args[0].random_id == send.call_args_list[1].args[0].random_id
    assert json.loads(queue(store)[0]["receipts"]) == [99]
    assert await restarted.deliver_pending() == 0
    assert send.await_count == 2


@pytest.mark.parametrize("cause", ["account", "review"])
async def test_sender_drift_and_required_review_never_silently_send(store, monkeypatch, cause):
    turn = await interrupted(store, destination=RecoveryDestination(77, 55, 42, review_required=cause == "review"))
    await finish(store, turn)
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 66 if cause == "account" else 55)
    send = AsyncMock(return_value=99)
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", send)
    await store.deliver_pending()
    assert (await store.delivery_status(turn))["state"] == "needs_review"
    send.assert_not_awaited()


async def test_recovered_approval_is_not_a_terminal_answer(store, monkeypatch):
    monkeypatch.setattr(settings, "tool_approvals_enabled", True)
    turn = await interrupted(store, claim=False)
    sender = Sender()
    value, _ = agent(
        store,
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[{"id": "c1", "name": sender.name, "args": {}}]),
            AIMessage(content="approved result"),
        ],
        tools=[sender],
    )
    assert await value.resume_abandoned_turns() == 0
    outcome = await value.get_turn_outcome(turn)
    assert outcome.status == "waiting_approval"
    assert sender.calls == 0
    notice = queue(store)[0]
    assert notice["event_key"] == f"turn:{turn}:approval:{outcome.approval_id}"
    assert (await store.delivery_status(turn))["state"] == "awaiting_result"
    await value.resolve_tool_approval(outcome.approval_id, True, "owner")
    assert sender.calls == 1
    assert (await value.get_turn_outcome(turn)).status == "completed"
    assert queue(store)[0]["obsolete"] == 1
    assert queue(store)[1]["event_key"] == f"turn:{turn}:result"
    await value.resolve_tool_approval(outcome.approval_id, True, "owner")
    assert sender.calls == 1 and len(queue(store)) == 2
    send = AsyncMock(return_value=11)
    monkeypatch.setattr("kronos.telegram_delivery.ready_sender", lambda: 55)
    monkeypatch.setattr("kronos.telegram_delivery.send_chunk", send)
    assert await store.deliver_pending() == 1
    assert send.await_count == 1
    assert "approved result" in send.call_args.args[0].text


async def test_expiry_requires_absent_owner_and_suppresses_old_notice(store):
    turn = await interrupted(store)
    approval = await store.create_pending_approval(
        turn_id=turn, thread_id=THREAD, tool_call_id="c1", tool_name="send_message", args={}
    )
    execute(store, "UPDATE pending_approvals SET requested_at = datetime('now', '-2 hours')")
    async with own_conversation(store.db_path, THREAD):
        assert await store.expire_recovery_approvals() == 0
        assert queue(store)[0]["obsolete"] == 0
    assert await store.expire_recovery_approvals() == 1
    assert (await store.get_turn_outcome(turn)).status == "expired"
    assert queue(store)[0]["obsolete"] == 1
    assert queue(store)[1]["state"] == "pending"
    assert await store.claim_pending_approval(approval_id=approval, decision="approved") is None
    assert await store.expire_recovery_approvals() == 0


async def test_expired_legacy_approval_without_delivery_request_is_not_adopted(store):
    turn = await interrupted(store, claim=False)
    await store.create_pending_approval(
        turn_id=turn, thread_id=THREAD, tool_call_id="c", tool_name="send_message", args={}
    )
    execute(store, "UPDATE pending_approvals SET requested_at = datetime('now', '-2 hours')")
    assert await store.expire_recovery_approvals() == 0
    assert queue(store) == []


async def test_retention_keeps_obligation_and_unknown_legacy_result_is_explicit(store):
    turn = await interrupted(store)
    await store.finish_turn(turn)
    assert (await store.delivery_status(turn))["state"] == "result_missing"
    execute(store, "UPDATE active_turns SET completed_at = datetime('now', '-60 days')")
    assert (await store.prune_turn_history())["turns"] == 0


async def test_worker_drains_sessions_while_plan_transport_stalls(monkeypatch):
    from kronos.cron import delivery

    real_sleep = asyncio.sleep
    second = asyncio.Event()
    calls = 0

    async def session_cycle():
        nonlocal calls
        calls += 1
        if calls == 2:
            second.set()

    async def blocked_plan():
        await asyncio.Event().wait()

    monkeypatch.setattr(delivery.plans, "deliver_pending", blocked_plan)
    monkeypatch.setattr(
        delivery,
        "asyncio",
        SimpleNamespace(
            TaskGroup=asyncio.TaskGroup,
            CancelledError=asyncio.CancelledError,
            current_task=asyncio.current_task,
            sleep=lambda n: real_sleep(0.01),
        ),
    )
    task = asyncio.create_task(delivery.run_delivery_worker(SimpleNamespace(deliver_pending=session_cycle)))
    try:
        await asyncio.wait_for(second.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cancellation_rolls_back_terminal_result_and_obligation(store, monkeypatch):
    import kronos.session as module

    turn = await interrupted(store)
    entered = asyncio.Event()
    original = module.queue_result

    async def paused(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "queue_result", paused)
    task = asyncio.create_task(finish(store, turn))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue(store) == []
    assert (await store.get_turn_detail(turn))["status"] == "resuming"
    assert await store.load(THREAD) == []


async def test_report_mode_closes_an_existing_recovery_duty_without_running_a_model(store):
    turn = await interrupted(store)
    assert await store.recover_abandoned_turns() == 1
    assert (await store.get_turn_outcome(turn)).status == "interrupted"
    assert (await store.delivery_status(turn))["state"] == "pending"
    assert "Восстановление не завершено" in queue(store)[0]["chunks"]


async def test_missing_approval_does_not_mutate_delivery(store):
    turn = await interrupted(store)
    assert await store.claim_pending_approval(approval_id="missing", decision="approved") is None
    assert (await store.get_turn_detail(turn))["status"] == "resuming"
    assert queue(store) == []


async def test_transport_cancel_does_not_silently_remove_a_worker(monkeypatch):
    from kronos.cron import delivery

    second = asyncio.Event()
    calls = 0
    real_sleep = asyncio.sleep

    async def cycle():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError()
        second.set()

    monkeypatch.setattr(delivery.plans, "deliver_pending", cycle)
    monkeypatch.setattr(
        delivery,
        "asyncio",
        SimpleNamespace(
            CancelledError=asyncio.CancelledError,
            current_task=asyncio.current_task,
            sleep=lambda n: real_sleep(0.01),
        ),
    )
    task = asyncio.create_task(delivery.run_delivery_worker())
    try:
        await asyncio.wait_for(second.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_concurrent_migration_preserves_legacy_unknown_routes(tmp_path):
    import aiosqlite

    from kronos.migrations.v008_turn_delivery import migrate

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE active_turns (turn_id TEXT, thread_id TEXT, status TEXT)")
        db.execute("INSERT INTO active_turns VALUES ('old', '77', 'done')")
    async with aiosqlite.connect(path) as first, aiosqlite.connect(path) as second:
        await asyncio.gather(migrate(first), migrate(second))
        await migrate(first)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT status, recovery_destination, delivery_requested, delivery_issue FROM active_turns"
        ).fetchone() == ("done", "", 0, "")
        assert db.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0


async def test_failed_migration_rolls_back_all_delivery_columns(tmp_path, monkeypatch):
    import aiosqlite

    from kronos.migrations.v008_turn_delivery import migrate

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE active_turns (turn_id TEXT)")
    async with aiosqlite.connect(path) as db:
        original = db.execute

        async def broken(sql, *args):
            if "ADD COLUMN delivery_requested" in sql:
                raise OSError("injected migration failure")
            return await original(sql, *args)

        # Preserve aiosqlite's async context-manager protocol on read queries.
        def execute(sql, *args):
            if "ADD COLUMN delivery_requested" in sql:
                return broken(sql, *args)
            return original(sql, *args)

        monkeypatch.setattr(db, "execute", execute)
        with pytest.raises(OSError):
            await migrate(db)
    with sqlite3.connect(path) as db:
        assert [r[1] for r in db.execute("PRAGMA table_info(active_turns)")] == ["turn_id"]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name = 'delivery_outbox'").fetchall()
