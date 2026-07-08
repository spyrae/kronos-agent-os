"""Live progress reporter (roadmap 4.1).

The reporter edits a throwaway draft message with tool progress. It must be
lazy (no draft for fast no-tool replies) and best-effort (never break a reply).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kronos import bridge


def test_humanize_tool_maps_known_and_unknown():
    assert "ищу" in bridge._humanize_tool("brave_web_search")
    assert "читаю" in bridge._humanize_tool("fetch_url")
    assert bridge._humanize_tool("weird_custom") == "🔧 weird_custom"


def test_progress_label_only_for_actionable_events():
    assert bridge._progress_label("tool_call", {"name": "brave"}) is not None
    assert bridge._progress_label("tool_result", {"name": "brave"}) is None
    assert bridge._progress_label("tool_approval_required", {}) == "⏸️ жду подтверждения…"


async def test_reporter_lazy_no_draft_without_events(monkeypatch):
    client = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        delete_messages=AsyncMock(),
    )
    monkeypatch.setattr(bridge, "_client", client)

    reporter = bridge._ProgressReporter(chat_id=1, topic_id=None).start()
    await asyncio.sleep(0.03)  # no events fired
    await reporter.finish()

    client.send_message.assert_not_called()  # no throwaway message flashed
    client.delete_messages.assert_not_called()


async def test_reporter_sends_draft_on_event_then_deletes(monkeypatch):
    draft = SimpleNamespace(id=555)
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=draft),
        edit_message=AsyncMock(),
        delete_messages=AsyncMock(),
    )
    monkeypatch.setattr(bridge, "_client", client)

    reporter = bridge._ProgressReporter(chat_id=1, topic_id=None).start()
    reporter.on_event("tool_call", {"name": "brave_search"})
    await asyncio.sleep(0.25)  # let the poll pick it up and render
    await reporter.finish()

    client.send_message.assert_awaited()  # draft materialized on first event
    client.delete_messages.assert_awaited_with(1, [draft])  # cleaned up


async def test_reporter_render_failure_never_raises(monkeypatch):
    client = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("telegram down")),
        edit_message=AsyncMock(),
        delete_messages=AsyncMock(),
    )
    monkeypatch.setattr(bridge, "_client", client)

    reporter = bridge._ProgressReporter(chat_id=1, topic_id=None).start()
    reporter.on_event("tool_call", {"name": "brave"})
    await asyncio.sleep(0.25)
    await reporter.finish()  # must not raise despite send_message failing
