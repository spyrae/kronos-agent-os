"""Idempotency for side-effecting tools (moat phase 10.1).

Durable resume re-runs the unanswered part of a turn. That is only safe if a call
that already happened is recognised as such — otherwise a crash between "message
sent" and "result journalled" sends it twice on recovery.
"""

import pytest
from langchain_core.tools import BaseTool, tool

from kronos.config import settings
from kronos.engine import execute_tool, side_effect_key, tool_has_side_effect
from kronos.security.effects import mark_side_effect
from kronos.session import SessionStore


class SendingTool(BaseTool):
    """Stands in for anything the outside world notices."""

    name: str = "send_message"
    description: str = "send a message"
    calls: int = 0

    def _run(self, **kwargs) -> str:
        self.calls += 1
        return f"sent #{self.calls}"


class FailingTool(BaseTool):
    name: str = "send_flaky"
    description: str = "send, badly"
    calls: int = 0

    def _run(self, **kwargs) -> str:
        self.calls += 1
        raise RuntimeError("smtp down")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    return SessionStore(str(tmp_path / "session.db"), agent_name="test")


def _sending() -> SendingTool:
    sender = SendingTool()
    mark_side_effect([sender])
    return sender


async def _run(tool, store, *, turn_id="turn-1", args=None, call_id="c1"):
    return await execute_tool(
        tool,
        {"id": call_id, "args": args or {"chat_id": 1, "text": "привет"}},
        get_external_effect=store.get_external_effect,
        record_external_effect=lambda key, name, result: store.record_external_effect(
            key=key, turn_id=turn_id, tool=name, result=result
        ),
        turn_id=turn_id,
    )


def test_marking_is_opt_in():
    @tool
    def plain() -> str:
        """Local tool."""
        return "ok"

    assert tool_has_side_effect(plain) is False
    mark_side_effect([plain])
    assert tool_has_side_effect(plain) is True


def test_key_is_stable_for_the_same_call():
    sender = _sending()
    args = {"chat_id": 1, "text": "привет"}

    assert side_effect_key(sender, args, "turn-1") == side_effect_key(sender, dict(args), "turn-1")


def test_key_differs_by_args_and_by_turn():
    """Within a turn a repeat is a retry; in a later turn it is a new request."""
    sender = _sending()

    same_turn = side_effect_key(sender, {"text": "a"}, "turn-1")
    other_args = side_effect_key(sender, {"text": "b"}, "turn-1")
    later_turn = side_effect_key(sender, {"text": "a"}, "turn-2")

    assert same_turn != other_args
    assert same_turn != later_turn


def test_custom_key_function_narrows_identity():
    """A regenerated request id must not make a retry look like a new effect."""
    sender = SendingTool()
    sender.metadata = {"side_effect": True, "idempotency_key": lambda args: f"chat:{args['chat_id']}:{args['text']}"}

    first = side_effect_key(sender, {"chat_id": 7, "text": "привет", "request_id": "abc"})
    second = side_effect_key(sender, {"chat_id": 7, "text": "привет", "request_id": "xyz"})

    assert first == second == "send_message:chat:7:привет"


def test_broken_key_function_falls_back_instead_of_failing(caplog):
    sender = SendingTool()
    sender.metadata = {"side_effect": True, "idempotency_key": lambda args: 1 / 0}

    key = side_effect_key(sender, {"text": "a"}, "turn-1")

    assert key.startswith("send_message:turn-1:")
    assert "idempotency_key callable failed" in caplog.text


@pytest.mark.asyncio
async def test_repeated_call_does_not_run_twice(store):
    sender = _sending()

    first = await _run(sender, store)
    second = await _run(sender, store)

    assert sender.calls == 1
    assert first.content == "sent #1"
    assert second.content == "sent #1"  # the recorded result, not a new send


@pytest.mark.asyncio
async def test_different_arguments_still_run(store):
    sender = _sending()

    await _run(sender, store, args={"chat_id": 1, "text": "первое"})
    await _run(sender, store, args={"chat_id": 1, "text": "второе"})

    assert sender.calls == 2


@pytest.mark.asyncio
async def test_a_later_turn_runs_again(store):
    """The user asking twice is not a retry."""
    sender = _sending()

    await _run(sender, store, turn_id="turn-1")
    await _run(sender, store, turn_id="turn-2")

    assert sender.calls == 2


@pytest.mark.asyncio
async def test_unmarked_tool_is_never_deduplicated(store):
    """Read-only tools must stay repeatable — dedup would serve stale data."""
    plain = SendingTool()
    plain.name = "get_status"

    await _run(plain, store)
    await _run(plain, store)

    assert plain.calls == 2


@pytest.mark.asyncio
async def test_failed_effect_is_not_recorded(store):
    """A send that raised did not happen, so a retry must be allowed."""
    failing = FailingTool()
    mark_side_effect([failing])

    first = await _run(failing, store)
    second = await _run(failing, store)

    assert failing.calls == 2
    assert "[ERROR]" in first.content or "smtp" in first.content
    assert first.content == second.content


@pytest.mark.asyncio
async def test_effects_are_listed_per_turn(store):
    sender = _sending()
    await _run(sender, store, turn_id="turn-A")
    await _run(sender, store, turn_id="turn-B", args={"chat_id": 2, "text": "b"})

    effects_a = await store.list_external_effects("turn-A")
    effects_b = await store.list_external_effects("turn-B")

    assert len(effects_a) == 1 and effects_a[0]["tool"] == "send_message"
    assert len(effects_b) == 1
    assert effects_a[0]["idempotency_key"] != effects_b[0]["idempotency_key"]


@pytest.mark.asyncio
async def test_recording_the_same_key_twice_reports_not_new(store):
    """Concurrent retries must not both conclude they are first."""
    created = await store.record_external_effect(key="k1", turn_id="t", tool="send_message", result="sent")
    again = await store.record_external_effect(key="k1", turn_id="t", tool="send_message", result="sent again")

    assert created is True
    assert again is False
    assert await store.get_external_effect("k1") == "sent"  # first result wins


@pytest.mark.asyncio
async def test_without_callbacks_behaviour_is_unchanged(store):
    """Paths that do not pass the ledger (sub-agents, cron) keep working."""
    sender = _sending()

    first = await execute_tool(sender, {"id": "c1", "args": {"text": "a"}})
    second = await execute_tool(sender, {"id": "c2", "args": {"text": "a"}})

    assert sender.calls == 2
    assert first.content == "sent #1" and second.content == "sent #2"


@pytest.mark.asyncio
async def test_side_effect_tools_are_marked_in_production():
    """Inventory: a new mutating tool must not slip in unmarked."""
    from kronos.tools.council import convene_council
    from kronos.tools.gateway_tools import mcp_add_server, mcp_remove_server
    from kronos.tools.handoff import handoff_to_agent
    from kronos.tools.memory_ask import ask_agent_memory
    from kronos.tools.reminders import cancel_scheduled_task, schedule_followup, schedule_task

    for tool_obj in (
        schedule_task,
        schedule_followup,
        cancel_scheduled_task,
        handoff_to_agent,
        convene_council,
        ask_agent_memory,
        mcp_add_server,
        mcp_remove_server,
    ):
        assert tool_has_side_effect(tool_obj), tool_obj.name
