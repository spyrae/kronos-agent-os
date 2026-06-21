from datetime import UTC, datetime, timedelta

from kronos.observer.commands import (
    handle_observer_command,
    ignore_peer,
    observer_status,
    render_observer_status,
    run_morning_digest,
    unignore_peer,
)
from kronos.observer.models import DialogSnapshot, ObserverRunResult, ObserverSourceKind
from kronos.observer.state import ObserverStateStore
from kronos.workspace import Workspace

NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _snapshot(peer_id: str = "1") -> DialogSnapshot:
    incoming_at = NOW - timedelta(hours=10)
    return DialogSnapshot(
        peer_id=peer_id,
        peer_title="Alice",
        last_message_id=11,
        unread_count=1,
        message_count=1,
        excerpt="Нужно обсудить договор",
        metadata={
            "last_incoming_at": incoming_at.isoformat(),
            "last_message_direction": "incoming",
            "recent_messages": [
                {
                    "id": 11,
                    "date": incoming_at.isoformat(),
                    "direction": "incoming",
                    "excerpt": "Нужно обсудить договор",
                }
            ],
        },
    )


def test_ignore_unignore_status_and_audit(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))

    peer = ignore_peer(
        "telegram:alice",
        "spam from alice@example.com",
        state_store=store,
        actor_id="owner@example.com",
    )

    state = store.load()
    assert peer == "telegram:alice"
    assert state.ignored_peers == {"telegram:alice"}
    assert state.ignored_peer_reasons == {"telegram:alice": "spam from ***@***.com"}

    status_text = render_observer_status(
        observer_status(
            state_store=store,
            enabled_jobs={"personal-observer": True, "daily-scope": True},
        )
    )

    assert "personal-observer: enabled" in status_text
    assert "telegram:alice (spam from ***@***.com)" in status_text
    assert "alice@example.com" not in status_text

    unignore_peer("telegram:alice", state_store=store, actor_id="owner@example.com")

    state = store.load()
    assert state.ignored_peers == set()
    assert state.ignored_peer_reasons == {}
    assert [run["metadata"]["command"] for run in store.list_runs()] == [
        "ignore_peer",
        "unignore_peer",
    ]


def test_status_does_not_include_raw_run_metadata(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))
    store.append_run(
        ObserverRunResult(
            source_kind=ObserverSourceKind.TELEGRAM_UNREAD_DIGEST,
            run_id="safe-run-id",
            metadata={"raw_message_text": "PRIVATE CHAT BODY SHOULD NOT APPEAR"},
        )
    )

    rendered = render_observer_status(observer_status(state_store=store))

    assert "telegram_unread_digest" in rendered
    assert "PRIVATE CHAT BODY SHOULD NOT APPEAR" not in rendered
    assert "raw_message_text" not in rendered
    assert "safe-run-id" not in rendered


async def test_run_morning_digest_dry_run_does_not_send_or_update_state(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))
    sent = []

    async def fake_scanner(
        client,
        state,
        *,
        limit_dialogs,
        limit_messages_per_dialog,
        dry_run,
    ):
        assert client == "client"
        assert state is store
        assert limit_dialogs == 3
        assert limit_messages_per_dialog == 4
        assert dry_run is True
        return [_snapshot()]

    def fake_sender(*args, **kwargs):
        sent.append((args, kwargs))
        raise AssertionError("dry-run must not send Telegram messages")

    result = await run_morning_digest(
        client="client",
        state_store=store,
        scanner=fake_scanner,
        sender=fake_sender,
        now=NOW,
        dry_run=True,
        limit_dialogs=3,
        limit_messages_per_dialog=4,
        threshold_hours=8,
    )

    assert result.sent is False
    assert len(result.snapshots) == 1
    assert len(result.debts) == 1
    assert "Alice" in result.body
    assert sent == []
    assert store.load().last_scan_at == {}
    assert store.load().last_digest_at == {}
    assert store.list_runs()[0]["metadata"]["command"] == "digest_dry_run"


async def test_handle_observer_debts_scans_dry_run_without_state_update(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))

    async def fake_scanner(
        client,
        state,
        *,
        limit_dialogs,
        limit_messages_per_dialog,
        dry_run,
        unread_only,
    ):
        assert client == "client"
        assert state is store
        assert dry_run is True
        assert unread_only is False
        return [_snapshot()]

    reply = await handle_observer_command(
        "/observer debts",
        client="client",
        state_store=store,
        scanner=fake_scanner,
        actor_id="owner",
    )

    assert reply is not None
    assert "Reply debts:" in reply
    assert "Alice" in reply
    assert store.load().last_seen_message_ids == {}
    assert store.list_runs()[0]["metadata"]["command"] == "debts"


async def test_handle_observer_commands_are_dm_only(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))

    reply = await handle_observer_command(
        "/observer status",
        state_store=store,
        is_dm=False,
        actor_id="owner",
    )

    assert reply is None
    assert store.list_runs() == []
