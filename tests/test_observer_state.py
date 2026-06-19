import json

import pytest

from kronos.observer.models import (
    CapturedItem,
    DialogSnapshot,
    ObserverRunResult,
    ObserverSourceKind,
)
from kronos.observer.state import ObserverState, ObserverStateStore
from kronos.workspace import Workspace


def test_observer_state_creates_dirs_and_safe_defaults(tmp_path):
    workspace = Workspace(tmp_path)
    store = ObserverStateStore(workspace)

    state = store.load()

    assert (tmp_path / "ops" / "observer").is_dir()
    assert state.schema_version == ObserverState.schema_version
    assert state.created_at
    assert state.updated_at
    assert state.dialog_cursors == {}
    assert state.last_seen_message_ids == {}
    assert state.ignored_peers == set()
    assert state.muted_peers == set()
    assert state.last_digest_at == {}


def test_observer_state_roundtrips_control_data(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))

    store.update_dialog("telegram:42", cursor="offset:100", last_seen_message_id=100)
    store.set_ignored("telegram:spam")
    store.set_muted("telegram:noisy")
    store.mark_digest("morning", "2026-06-19T08:00:00Z")

    reloaded = ObserverStateStore(Workspace(tmp_path)).load()

    assert reloaded.dialog_cursors == {"telegram:42": "offset:100"}
    assert reloaded.last_seen_message_ids == {"telegram:42": 100}
    assert reloaded.ignored_peers == {"telegram:spam"}
    assert reloaded.muted_peers == {"telegram:noisy"}
    assert reloaded.last_digest_at == {"morning": "2026-06-19T08:00:00Z"}

    payload = json.loads((tmp_path / "ops" / "observer" / "state.json").read_text())
    assert payload["ignored_peers"] == ["telegram:spam"]
    assert payload["muted_peers"] == ["telegram:noisy"]


def test_observer_run_log_is_append_only_and_sanitized(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))

    store.append_run(
        ObserverRunResult(
            source_kind=ObserverSourceKind.TELEGRAM_UNREAD_DIGEST,
            run_id="morning-1",
            scanned_count=2,
            metadata={"owner": "alice@example.com"},
        )
    )
    store.append_run(
        ObserverRunResult(
            source_kind=ObserverSourceKind.TELEGRAM_REPLY_DEBT,
            run_id="debts-1",
            error_count=1,
            errors=("failed for bob@example.com",),
        )
    )

    runs = store.list_runs()

    assert [run["run_id"] for run in runs] == ["morning-1", "debts-1"]
    assert runs[0]["source_kind"] == "telegram_unread_digest"
    assert runs[0]["metadata"] == {"owner": "***@***.com"}
    assert runs[1]["errors"] == ["failed for ***@***.com"]
    assert (tmp_path / "ops" / "observer" / "runs.jsonl").read_text().count("\n") == 2


def test_observer_models_are_json_friendly_and_encode_source_rules():
    capture = CapturedItem(
        content="remember to call Alice",
        source_kind=ObserverSourceKind.TELEGRAM_TEXT_CAPTURE,
        source_message_id=123,
    )
    snapshot = DialogSnapshot(
        peer_id="telegram:alice",
        summary="Alice asked about the contract.",
        excerpt="contract?",
        unread_count=1,
    )

    assert capture.to_dict()["source_kind"] == "telegram_text_capture"
    assert CapturedItem.from_dict(capture.to_dict()) == capture
    assert snapshot.to_dict()["source_kind"] == "telegram_unread_digest"
    assert DialogSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert ObserverSourceKind.TELEGRAM_TEXT_CAPTURE.allows_raw_content is True
    assert ObserverSourceKind.TELEGRAM_UNREAD_DIGEST.allows_raw_content is False


def test_observer_state_rejects_unknown_schema_version(tmp_path):
    store = ObserverStateStore(Workspace(tmp_path))
    store.state_path.write_text('{"schema_version": 999}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported observer state schema_version"):
        store.load()
