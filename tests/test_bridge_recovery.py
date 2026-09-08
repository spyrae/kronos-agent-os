"""Queued approvals cannot change their owner, destination, topic or account."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from kronos import bridge
from kronos.bridge_recovery import handle_recovery_approval_command, recovery_decision_allowed, recovery_route
from kronos.config import settings
from tests.test_turn_delivery import ROUTE, THREAD, agent, interrupted, queue
from tests.test_turn_delivery import store as store


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    for name, value in (
        ("_agent", None),
        ("_client", None),
        ("_my_id", 55),
        ("_group_router", None),
        ("_my_username", ""),
    ):
        monkeypatch.setattr(bridge, name, value)
    monkeypatch.setattr(settings, "allowed_users", "101")
    monkeypatch.setattr(settings, "allow_all_users", True)


async def pending_turn(store):
    turn = await interrupted(store)
    approval = await store.create_pending_approval(
        turn_id=turn, thread_id=THREAD, tool_call_id="c", tool_name="send_message", args={}
    )
    return turn, approval, await store.get_pending_approval(approval)


@pytest.mark.parametrize("changed", [{}, {"sender_id": 202}, {"chat_id": 88}, {"topic_id": 43}, {"topic_id": None}])
async def test_approval_requires_owner_and_exact_route(store, changed):
    _, _, pending = await pending_turn(store)
    args = {"sender_id": 101, "chat_id": 77, "topic_id": 42} | changed
    assert await recovery_decision_allowed(SimpleNamespace(session_store=store), pending, **args) is (not changed)


async def test_account_changed_or_corrupt_pending_thread_is_denied(store, monkeypatch):
    _, _, pending = await pending_turn(store)
    value = SimpleNamespace(session_store=store)
    monkeypatch.setattr(bridge, "_my_id", 66)
    assert not await recovery_decision_allowed(value, pending, sender_id=101, chat_id=77, topic_id=42)
    monkeypatch.setattr(bridge, "_my_id", 55)
    pending["thread_id"] = "wrong"
    assert not await recovery_decision_allowed(value, pending, sender_id=101, chat_id=77, topic_id=42)


async def test_command_resolves_without_direct_duplicate_send(store, monkeypatch):
    turn, approval, _ = await pending_turn(store)

    async def resolve(approval_id, approved, decided_by):
        assert (approval_id, approved, decided_by) == (approval, True, "101")
        await store.claim_pending_approval(approval_id=approval, decision="approved")
        await store.finalize_turn(thread_id=THREAD, turn_id=turn, messages=[], content="result")
        return "result"

    value = SimpleNamespace(
        session_store=store, get_pending_tool_approval=store.get_pending_approval, resolve_tool_approval=resolve
    )
    monkeypatch.setattr(bridge, "_agent", value)
    event = SimpleNamespace(
        raw_text=f"/approve {approval}", sender_id=101, chat_id=77, is_private=False, respond=AsyncMock()
    )
    monkeypatch.setattr(bridge, "_extract_topic_id", lambda event: 42)
    assert await handle_recovery_approval_command(event)
    event.respond.assert_not_awaited()
    assert (await store.delivery_status(turn))["state"] == "pending"
    assert len(queue(store)) == 2


async def test_callback_uses_actual_topic_and_does_not_send_queue_owned_result(store, monkeypatch):
    from tests.test_observer_bridge_capture import _registered_message_handler

    turn, approval, pending = await pending_turn(store)
    client, _ = await _registered_message_handler(monkeypatch)
    resolve = AsyncMock(return_value="queue-owned result")
    value = SimpleNamespace(
        session_store=store, get_pending_tool_approval=AsyncMock(return_value=pending), resolve_tool_approval=resolve
    )
    monkeypatch.setattr(bridge, "_agent", value)
    monkeypatch.setattr(bridge, "_my_id", 55)
    event = SimpleNamespace(
        data=bridge._approval_callback_data("approve", approval),
        sender_id=101,
        chat_id=77,
        is_private=False,
        answer=AsyncMock(),
        respond=AsyncMock(),
        message=SimpleNamespace(reply_to=SimpleNamespace(reply_to_top_id=43, forum_topic=True, reply_to_msg_id=43)),
    )
    callback = client.handlers["handle_approval_callback"]
    await callback(event)
    resolve.assert_not_awaited()
    event.message.reply_to.reply_to_top_id = 42
    event.message.reply_to.reply_to_msg_id = 42
    await callback(event)
    resolve.assert_awaited_once()
    event.respond.assert_not_awaited()
    assert client.sent == []


async def test_transport_passes_frozen_route_and_dissent_requirement(store, monkeypatch):
    value, _ = agent(store, monkeypatch, [AIMessage(content="answer")])
    monkeypatch.setattr(bridge, "_agent", value)
    monkeypatch.setattr(
        "kronos.swarm_config.all_profiles", lambda: {settings.agent_name: SimpleNamespace(dissent="require")}
    )
    assert await bridge._ask_agent("question", 77, 101, topic_id=42, recovery_owner_review=True) == "answer"
    turn = (await store.list_turns())[0]
    route = await store.recovery_destination(turn["turn_id"])
    assert route.review_required and route.sender_id == ROUTE.sender_id
    assert queue(store) == []
    monkeypatch.setattr(bridge, "_my_id", None)
    assert recovery_route(77, 42) is None
