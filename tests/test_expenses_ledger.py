import pytest

from kronos.cron.expenses.ledger import ExpenseLedger
from kronos.db import SafeDB


@pytest.fixture
def ledger(tmp_path):
    return ExpenseLedger(SafeDB(tmp_path / "expenses_ledger.db"))


def test_is_processed_false_for_unknown(ledger):
    assert ledger.is_processed("msg-1") is False


def test_record_marks_processed(ledger):
    ledger.record(
        message_id="msg-1",
        source="grab",
        status="recorded",
        amount_idr=41500,
        expense_date="2026-07-05",
    )
    assert ledger.is_processed("msg-1") is True


def test_error_status_is_retried(ledger):
    ledger.record(message_id="msg-1", source="grab", status="error", error="boom")
    # error is non-terminal — the message must be reprocessed next run
    assert ledger.is_processed("msg-1") is False


def test_record_upsert_updates_row(ledger):
    ledger.record(message_id="msg-1", source="grab", status="error", error="boom")
    ledger.record(
        message_id="msg-1",
        source="grab",
        status="recorded",
        amount_idr=41500,
        expense_date="2026-07-05",
        notion_page_id="page-1",
    )
    row = ledger.get("msg-1")
    assert row["status"] == "recorded"
    assert row["notion_page_id"] == "page-1"
    assert row["error"] is None


def test_invalid_status_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.record(message_id="m", source="grab", status="bogus")


def test_mark_archived(ledger):
    ledger.record(
        message_id="msg-1",
        source="grab",
        status="recorded",
        amount_idr=41500,
        expense_date="2026-07-05",
    )
    ledger.mark_archived("msg-1")
    row = ledger.get("msg-1")
    assert row["archived"] == 1
    assert row["status"] == "archived"


def test_find_recorded_duplicate_cross_source(ledger):
    # Grab records the charge first; the bank's copy of the same charge must
    # find it and be recognised as a duplicate.
    ledger.record(
        message_id="grab-1",
        source="grab",
        status="recorded",
        amount_idr=41500,
        expense_date="2026-07-05",
    )
    dup = ledger.find_recorded_duplicate(41500, "2026-07-05")
    assert dup is not None
    assert dup["message_id"] == "grab-1"


def test_find_recorded_duplicate_none_when_no_match(ledger):
    ledger.record(
        message_id="grab-1",
        source="grab",
        status="recorded",
        amount_idr=41500,
        expense_date="2026-07-05",
    )
    assert ledger.find_recorded_duplicate(99999, "2026-07-05") is None
    assert ledger.find_recorded_duplicate(41500, "2026-07-06") is None
    assert ledger.find_recorded_duplicate(None, "2026-07-05") is None


def test_find_recorded_duplicate_ignores_skipped(ledger):
    # A skipped (non-expense) row must not shadow a later real charge.
    ledger.record(
        message_id="m-skip",
        source="permata",
        status="skipped",
        amount_idr=41500,
        expense_date="2026-07-05",
    )
    assert ledger.find_recorded_duplicate(41500, "2026-07-05") is None


def test_pending_lifecycle(ledger):
    pid = ledger.add_pending(
        message_id="msg-1",
        source="permata",
        description="ATM Bali",
        amount=500000,
        currency="IDR",
        amount_idr=500000,
        expense_date="2026-07-05",
        guessed_category="Other",
        reason="low confidence",
    )
    assert isinstance(pid, int)
    assert ledger.has_pending("msg-1") is True

    pending = ledger.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == pid

    ledger.resolve_pending(pid, "Health")
    assert ledger.list_pending() == []
    assert ledger.has_pending("msg-1") is False

    row = ledger.get_pending(pid)
    assert row["status"] == "resolved"
    assert row["resolved_category"] == "Health"


def test_pending_discard(ledger):
    pid = ledger.add_pending(
        message_id="msg-2",
        source="wondr",
        description="?",
        amount=None,
        currency=None,
        amount_idr=None,
        expense_date=None,
        guessed_category=None,
        reason="unparseable",
    )
    ledger.discard_pending(pid)
    assert ledger.list_pending() == []
    assert ledger.get_pending(pid)["status"] == "discarded"


def test_meta_watermark(ledger):
    assert ledger.get_meta("last_processed_email_ts") is None
    ledger.set_meta("last_processed_email_ts", "2026-07-05T12:00:00")
    assert ledger.get_meta("last_processed_email_ts") == "2026-07-05T12:00:00"
    ledger.set_meta("last_processed_email_ts", "2026-07-05T13:00:00")
    assert ledger.get_meta("last_processed_email_ts") == "2026-07-05T13:00:00"


def test_item_snapshot_and_claim_survive_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from kronos.cron.expenses.extract import ExtractedExpense

    path = tmp_path / "ledger.db"
    one = ExpenseLedger(SafeDB(path))
    two = ExpenseLedger(SafeDB(path))
    exp = ExtractedExpense("A", 100, "IDR", "Food", 0.9, "2026-07-05")
    one.prepare_items("m", "grab", [exp])
    two.prepare_items("m", "grab", [ExtractedExpense("changed", 999, "USD", "Food", 0.9)])
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda ledger: ledger.claim_item("m", 0), [one, two]))
    assert sorted(claims) == [False, True]
    assert '"description": "A"' in two.list_items("m")[0]["expense_json"]
    reopened = ExpenseLedger(SafeDB(path))
    assert not reopened.needs_processing("m")
    assert len(reopened.uncertain_items()) == 1
    assert reopened.finalize_message("m", "grab") == "error"


def test_migration_preserves_legacy_data_and_is_idempotent(tmp_path):
    from kronos.cron.expenses.ledger import _SCHEMA

    db = SafeDB(tmp_path / "legacy.db")
    db.init_schema(lambda conn: conn.executescript(_SCHEMA))
    db.write(
        "INSERT INTO processed_emails(message_id, source, status, processed_at, updated_at) "
        "VALUES ('legacy', 'grab', 'recorded', 'before', 'before')"
    )
    ExpenseLedger(db)
    again = ExpenseLedger(db)
    assert again.is_processed("legacy")
    assert again.get("legacy")["updated_at"] == "before"
    assert again.list_items("legacy") == []
    assert again.get_meta("schema_expense_items") == "1"
