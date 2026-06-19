from datetime import UTC, datetime, timedelta

from kronos.observer.daily_scope import (
    build_daily_scope,
    local_day_window,
    save_daily_scope,
)
from kronos.observer.models import DialogSnapshot
from kronos.observer.render import render_daily_scope
from kronos.workspace import Workspace

NOW = datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _snapshot(peer_id="1", *, peer_title="Alice", last_direction="incoming"):
    today = NOW - timedelta(hours=2)
    yesterday = NOW - timedelta(days=1)
    return DialogSnapshot(
        peer_id=peer_id,
        peer_title=peer_title,
        unread_count=0,
        excerpt="договорились созвониться",
        metadata={
            "recent_messages": [
                {
                    "id": 3,
                    "date": today.isoformat(),
                    "direction": last_direction,
                    "excerpt": "договорились созвониться по KAOS",
                },
                {
                    "id": 2,
                    "date": today.isoformat(),
                    "direction": "outgoing",
                    "excerpt": "скинь финальный план",
                },
                {
                    "id": 1,
                    "date": yesterday.isoformat(),
                    "direction": "incoming",
                    "excerpt": "старое сообщение",
                },
            ]
        },
    )


def test_local_day_window_uses_utc_plus_8_day():
    start, end = local_day_window(NOW)

    assert start.isoformat() == "2026-06-18T16:00:00+00:00"
    assert end.isoformat() == "2026-06-19T16:00:00+00:00"


def test_build_daily_scope_groups_by_contact_and_filters_local_day():
    entries = build_daily_scope([_snapshot()], NOW)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.peer_title == "Alice"
    assert "старое сообщение" not in entry.summary
    assert "договорились" in entry.summary
    assert entry.metadata["message_count"] == 2
    assert entry.metadata["agreements"]


def test_incoming_last_message_creates_risk_flag_and_render_output():
    entry = build_daily_scope([_snapshot(last_direction="incoming")], NOW)[0]

    assert entry.metadata["risk"] is True
    body = render_daily_scope([entry], generated_at=NOW)
    assert "🌙 Карта дня" in body
    assert "нет ответа на последнее входящее" in body


def test_outgoing_last_message_has_no_risk():
    entry = build_daily_scope([_snapshot(last_direction="outgoing")], NOW)[0]

    assert entry.metadata["risk"] is False


def test_save_daily_scope_creates_expected_file(tmp_path):
    workspace = Workspace(tmp_path)
    entries = build_daily_scope([_snapshot()], NOW)

    path = save_daily_scope(entries, workspace=workspace, day=NOW)

    assert path == tmp_path / "notes" / "user" / "daily-scope" / "2026-06-19.md"
    assert "Daily Scope" in path.read_text(encoding="utf-8")
