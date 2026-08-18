import pytest

from kronos.competitors.store import CompetitorStore
from kronos.competitors.tracker import CompetitiveTracker
from kronos.config import settings


@pytest.fixture
def tracker(tmp_path, monkeypatch) -> CompetitiveTracker:
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))

    from kronos import db as database

    database._instances.clear()
    return CompetitiveTracker()


def test_update_preserves_fields_that_are_not_supplied(tracker: CompetitiveTracker) -> None:
    tracker.update(
        "ai_chat",
        our_status="leader",
        competitor_leader="Rival",
        notes="Original note",
        trend="improving",
    )
    tracker.update("ai_chat", notes="Updated note")

    row = next(item for item in tracker.get_all() if item["feature_area"] == "ai_chat")
    assert row["our_status"] == "leader"
    assert row["competitor_leader"] == "Rival"
    assert row["notes"] == "Updated note"
    assert row["trend"] == "improving"


def test_mark_digested_marks_only_selected_changes(tracker: CompetitiveTracker) -> None:
    store = CompetitorStore()
    snapshot_id = store.save_snapshot("rival", "app_store", {"rating": 4.5})
    first_change = store.save_change("rival", "app_store", "pricing", "info", "New price")
    second_change = store.save_change("rival", "app_store", "feature", "info", "New feature")
    untouched_change = store.save_change("rival", "app_store", "review", "info", "New review")

    store.mark_digested([first_change, second_change])

    assert snapshot_id > 0
    assert store.get_latest_snapshot("rival", "app_store") == {"rating": 4.5}
    assert [item["id"] for item in store.get_undigested_changes()] == [untouched_change]
