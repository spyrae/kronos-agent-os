from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from kronos.observer.state import ObserverStateStore
from kronos.observer.telegram_scan import SCAN_NAME, scan_private_dialogs
from kronos.workspace import Workspace


class FakeDialog:
    def __init__(
        self,
        *,
        entity,
        unread_count=1,
        is_user=True,
        is_group=False,
        is_channel=False,
        name="",
    ):
        self.entity = entity
        self.unread_count = unread_count
        self.is_user = is_user
        self.is_group = is_group
        self.is_channel = is_channel
        self.name = name


class FakeMessage:
    def __init__(self, *, message_id, text, out=False, date=None):
        self.id = message_id
        self.message = text
        self.out = out
        self.date = date or datetime(2026, 6, 19, 8, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self, dialogs, messages_by_peer):
        self.dialogs = dialogs
        self.messages_by_peer = messages_by_peer
        self.read_ack_calls = []
        self.send_calls = []

    async def iter_dialogs(self, limit):
        for dialog in self.dialogs[:limit]:
            yield dialog

    async def iter_messages(self, entity, limit):
        for message in self.messages_by_peer.get(entity.id, [])[:limit]:
            yield message

    async def send_read_acknowledge(self, *args, **kwargs):
        self.read_ack_calls.append((args, kwargs))
        raise AssertionError("scanner must not call send_read_acknowledge")

    async def send_message(self, *args, **kwargs):
        self.send_calls.append((args, kwargs))
        raise AssertionError("scanner must not send messages")


def _user(user_id, *, first_name="Alice", username="alice", bot=False):
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name="",
        username=username,
        bot=bot,
        is_self=False,
        megagroup=False,
        broadcast=False,
    )


async def test_scan_private_dialog_includes_unread_snapshot_and_updates_state(tmp_path):
    workspace = Workspace(tmp_path)
    store = ObserverStateStore(workspace)
    user = _user(1, first_name="Alice", username="alice")
    client = FakeClient(
        [FakeDialog(entity=user, unread_count=2)],
        {
            1: [
                FakeMessage(message_id=11, text="ping from alice@example.com"),
                FakeMessage(message_id=10, text="my outgoing reply", out=True),
            ]
        },
    )

    snapshots = await scan_private_dialogs(
        client,
        store,
        limit_dialogs=10,
        limit_messages_per_dialog=5,
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.peer_id == "1"
    assert snapshot.peer_title == "Alice"
    assert snapshot.unread_count == 2
    assert snapshot.last_message_id == 11
    assert snapshot.metadata["username"] == "alice"
    assert snapshot.metadata["last_message_direction"] == "incoming"
    assert snapshot.metadata["last_incoming_at"]
    assert snapshot.metadata["last_outgoing_at"]
    assert snapshot.metadata["recent_messages"][0]["direction"] == "incoming"
    assert "***@***.com" in snapshot.excerpt

    state = store.load()
    assert state.last_seen_message_ids == {"1": 11}
    assert state.dialog_cursors == {"1": "msg:11"}
    assert state.last_scan_at[SCAN_NAME]
    assert store.list_runs()[0]["captured_count"] == 1
    assert client.read_ack_calls == []
    assert client.send_calls == []


async def test_scan_excludes_groups_channels_bots_ignored_and_muted(tmp_path):
    workspace = Workspace(tmp_path)
    store = ObserverStateStore(workspace)
    store.set_ignored("2")
    store.set_muted("3")

    included = _user(1, first_name="Included")
    ignored = _user(2, first_name="Ignored")
    muted = _user(3, first_name="Muted")
    bot = _user(4, first_name="Bot", bot=True)
    group = SimpleNamespace(id=5, title="Group", megagroup=True, broadcast=False, bot=False)
    channel = SimpleNamespace(id=6, title="Channel", megagroup=False, broadcast=True, bot=False)
    client = FakeClient(
        [
            FakeDialog(entity=included, unread_count=1),
            FakeDialog(entity=ignored, unread_count=1),
            FakeDialog(entity=muted, unread_count=1),
            FakeDialog(entity=bot, unread_count=1),
            FakeDialog(entity=group, unread_count=1, is_user=False, is_group=True),
            FakeDialog(entity=channel, unread_count=1, is_user=False, is_channel=True),
        ],
        {
            1: [FakeMessage(message_id=1, text="hello")],
            2: [FakeMessage(message_id=2, text="ignored")],
            3: [FakeMessage(message_id=3, text="muted")],
            4: [FakeMessage(message_id=4, text="bot")],
            5: [FakeMessage(message_id=5, text="group")],
            6: [FakeMessage(message_id=6, text="channel")],
        },
    )

    snapshots = await scan_private_dialogs(
        client,
        store,
        limit_dialogs=10,
        limit_messages_per_dialog=3,
    )

    assert [snapshot.peer_id for snapshot in snapshots] == ["1"]
    assert store.load().last_seen_message_ids == {"1": 1}


async def test_scan_respects_since_and_dry_run_does_not_update_state(tmp_path):
    workspace = Workspace(tmp_path)
    store = ObserverStateStore(workspace)
    user = _user(1)
    now = datetime(2026, 6, 19, 8, 0, tzinfo=UTC)
    client = FakeClient(
        [FakeDialog(entity=user, unread_count=1)],
        {
            1: [
                FakeMessage(message_id=3, text="fresh", date=now),
                FakeMessage(message_id=2, text="old", date=now - timedelta(days=2)),
            ]
        },
    )

    snapshots = await scan_private_dialogs(
        client,
        store,
        limit_dialogs=10,
        limit_messages_per_dialog=5,
        since=now - timedelta(hours=1),
        dry_run=True,
    )

    assert len(snapshots) == 1
    assert snapshots[0].message_count == 1
    assert store.load().last_seen_message_ids == {}
    assert store.list_runs() == []
