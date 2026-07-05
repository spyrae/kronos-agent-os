import pytest

from kronos.config import settings
from kronos.cron.expenses import processor as proc
from kronos.cron.expenses.extract import AuditVerdict, ExtractedExpense
from kronos.cron.expenses.gmail import EmailMessage
from kronos.cron.expenses.ledger import ExpenseLedger
from kronos.db import SafeDB


class FakeGmail:
    def __init__(self, by_source, messages, archive_ok=True):
        self.by_source = by_source          # {"grab": [{"message_id": "g1"}], ...}
        self.messages = messages            # {"g1": EmailMessage(...)}
        self.archive_ok = archive_ok
        self.archived = []
        self.searched = []

    async def search(self, query, limit=25):
        self.searched.append(query)
        for src, refs in self.by_source.items():
            if src in query:
                return refs
        return []

    async def fetch(self, ids):
        return [self.messages[i] for i in ids if i in self.messages]

    async def archive(self, message_id):
        self.archived.append(message_id)
        return self.archive_ok


class Writer:
    def __init__(self, result="✅ ok"):
        self.result = result
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        return self.result


def _extractor(mapping):
    def _ex(email, model=None):
        return list(mapping.get(email.message_id, []))
    return _ex


def _auditor(ok=True, category=None):
    def _au(text, exp, model=None):
        return AuditVerdict(
            ok=ok, is_expense=ok, amount_matches=ok,
            category=category or exp.category, confidence=0.9,
            issues="" if ok else "amount mismatch",
        )
    return _au


@pytest.fixture
def ledger(tmp_path):
    return ExpenseLedger(SafeDB(tmp_path / "expenses_ledger.db"))


@pytest.fixture
def notes():
    captured = []

    def notifier(text, topic_id=None):
        captured.append((text, topic_id))

    notifier.captured = captured
    return notifier


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "notion_api_key", "ntn_test")
    monkeypatch.setenv("NOTION_EXPENSES_DB_ID", "db-test")


async def _run(gmail, ledger, notes, *, mapping, auditor=None, writer=None):
    writer = writer or Writer()
    result = await proc.run_email_expenses(
        gmail_client=gmail, ledger=ledger,
        extractor=_extractor(mapping), auditor=auditor or _auditor(ok=True),
        expense_writer=writer, notifier=notes,
    )
    return result, writer


@pytest.mark.asyncio
async def test_happy_path_records_and_archives(ledger, notes, monkeypatch):
    monkeypatch.setenv("EMAIL_EXPENSES_ARCHIVE", "true")
    gmail = FakeGmail(
        {"grab": [{"message_id": "g1", "thread_id": ""}]},
        {"g1": EmailMessage("g1", "Grab receipt 41,500 IDR", "grab")},
    )
    mapping = {"g1": [ExtractedExpense("GrabFood", 41500, "IDR", "Food", 0.9, "2026-07-05")]}

    counts, writer = await _run(gmail, ledger, notes, mapping=mapping)

    assert counts["recorded"] == 1
    assert counts["archived"] == 1
    assert writer.calls[0]["description"] == "GrabFood"
    assert writer.calls[0]["ref"] == "g1"
    assert gmail.archived == ["g1"]
    assert ledger.is_processed("g1") is True
    assert notes.captured and "Записано: 1" in notes.captured[0][0]


@pytest.mark.asyncio
async def test_archiving_disabled_by_default_keeps_email(ledger, notes, monkeypatch):
    monkeypatch.delenv("EMAIL_EXPENSES_ARCHIVE", raising=False)  # default OFF
    gmail = FakeGmail(
        {"grab": [{"message_id": "g1"}]},
        {"g1": EmailMessage("g1", "Grab receipt 41,500 IDR", "grab")},
    )
    mapping = {"g1": [ExtractedExpense("GrabFood", 41500, "IDR", "Food", 0.9, "2026-07-05")]}

    counts, writer = await _run(gmail, ledger, notes, mapping=mapping)

    assert counts["recorded"] == 1
    assert counts["archived"] == 0          # not archived
    assert gmail.archived == []             # inbox untouched
    assert len(writer.calls) == 1           # but still written to Notion
    assert ledger.is_processed("g1") is True  # ledger still guards re-runs
    assert ledger.get("g1")["status"] == "recorded"  # not 'archived'
    assert "Архив: выкл" in notes.captured[0][0]


@pytest.mark.asyncio
async def test_cross_source_duplicate_not_written_twice(ledger, notes, monkeypatch):
    monkeypatch.setenv("EMAIL_EXPENSES_ARCHIVE", "true")
    gmail = FakeGmail(
        {"grab": [{"message_id": "g1"}], "wondr": [{"message_id": "w1"}]},
        {
            "g1": EmailMessage("g1", "Grab 41,500 IDR", "grab"),
            "w1": EmailMessage("w1", "Wondr debit 41,500 IDR", "wondr"),
        },
    )
    mapping = {
        "g1": [ExtractedExpense("GrabFood", 41500, "IDR", "Food", 0.9, "2026-07-05")],
        "w1": [ExtractedExpense("Card debit", 41500, "IDR", "Other", 0.9, "2026-07-05")],
    }

    counts, writer = await _run(gmail, ledger, notes, mapping=mapping)

    assert counts["recorded"] == 1        # only Grab written
    assert counts["duplicates"] == 1      # bank copy deduped
    assert len(writer.calls) == 1
    assert writer.calls[0]["ref"] == "g1"
    assert set(gmail.archived) == {"g1", "w1"}  # both archived


@pytest.mark.asyncio
async def test_low_confidence_goes_to_pending(ledger, notes):
    gmail = FakeGmail(
        {"permata": [{"message_id": "p1"}]},
        {"p1": EmailMessage("p1", "Permata debit IDR 500,000 at ATM", "permata")},
    )
    mapping = {"p1": [ExtractedExpense("ATM withdrawal", 500000, "IDR", "Other", 0.2, "2026-07-05")]}

    counts, writer = await _run(gmail, ledger, notes, mapping=mapping)

    assert counts["pending"] == 1
    assert counts["recorded"] == 0
    assert writer.calls == []             # not written
    assert gmail.archived == []           # not archived until resolved
    assert ledger.is_processed("p1") is False
    assert ledger.has_pending("p1") is True
    report = notes.captured[0][0]
    assert "Куда отнести эти траты?" in report
    pid = ledger.list_pending()[0]["id"]
    assert f"#{pid}" in report            # pending shown WITH its id so it can be resolved
    assert "ATM withdrawal" in report


@pytest.mark.asyncio
async def test_reasks_open_pending_from_previous_runs(ledger, notes):
    # A pending left over from an earlier run (user never answered)
    old = ledger.add_pending(
        message_id="old1", source="wondr", description="PEYIA BALI",
        amount=494340, currency="IDR", amount_idr=494340, expense_date="2026-07-05",
        guessed_category="Other", reason="low category confidence",
    )
    # This run finds nothing new — the report must still re-ask the old pending.
    gmail = FakeGmail({"grab": []}, {})
    counts, writer = await _run(gmail, ledger, notes, mapping={})

    report = notes.captured[0][0]
    assert "Куда отнести эти траты?" in report
    assert f"#{old}" in report
    assert "PEYIA BALI" in report


@pytest.mark.asyncio
async def test_audit_rejection_goes_to_pending(ledger, notes):
    gmail = FakeGmail(
        {"grab": [{"message_id": "g1"}]},
        {"g1": EmailMessage("g1", "Grab 41,500 IDR", "grab")},
    )
    mapping = {"g1": [ExtractedExpense("GrabFood", 999999, "IDR", "Food", 0.95, "2026-07-05")]}

    counts, writer = await _run(gmail, ledger, notes, mapping=mapping, auditor=_auditor(ok=False))

    assert counts["pending"] == 1
    assert counts["recorded"] == 0
    assert writer.calls == []
    assert gmail.archived == []
    assert ledger.has_pending("g1") is True


async def test_non_expense_is_skipped(ledger, notes):
    gmail = FakeGmail(
        {"wondr": [{"message_id": "w1"}]},
        {"w1": EmailMessage("w1", "Your salary top-up arrived", "wondr")},
    )
    counts, writer = await _run(gmail, ledger, notes, mapping={"w1": []})

    assert counts["skipped"] == 1
    assert counts["recorded"] == 0
    assert gmail.archived == []
    assert ledger.is_processed("w1") is True   # handled (skipped)
    # A report is ALWAYS posted so the run is visible, even with nothing recorded.
    assert notes.captured
    assert "Записано: 0" in notes.captured[0][0]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_reports(ledger, notes):
    gmail = FakeGmail(
        {"grab": [{"message_id": "g1"}]},
        {"g1": EmailMessage("g1", "Grab 41,500 IDR", "grab")},
    )
    mapping = {"g1": [ExtractedExpense("GrabFood", 41500, "IDR", "Food", 0.9, "2026-07-05")]}
    writer = Writer()

    counts = await proc.run_email_expenses(
        gmail_client=gmail, ledger=ledger,
        extractor=_extractor(mapping), auditor=_auditor(ok=True),
        expense_writer=writer, notifier=notes, dry_run=True,
    )

    assert counts["recorded"] == 1          # would record
    assert writer.calls == []               # but nothing written
    assert gmail.archived == []             # nothing archived
    assert ledger.is_processed("g1") is False   # ledger untouched
    assert ledger.has_pending("g1") is False
    report = notes.captured[0][0]
    assert "DRY-RUN" in report
    assert "🔎" in report and "GrabFood" in report


@pytest.mark.asyncio
async def test_unsupported_currency_goes_to_pending(ledger, notes):
    gmail = FakeGmail(
        {"grab": [{"message_id": "g1"}]},
        {"g1": EmailMessage("g1", "Grab 40 MYR", "grab")},
    )
    mapping = {"g1": [ExtractedExpense("Grab KL", 40, "MYR", "Transport", 0.9, "2026-07-05")]}

    counts, writer = await _run(gmail, ledger, notes, mapping=mapping)

    assert counts["pending"] == 1
    assert writer.calls == []
    assert ledger.has_pending("g1") is True


@pytest.mark.asyncio
async def test_skips_when_not_kronos(monkeypatch, ledger, notes):
    monkeypatch.setattr(settings, "agent_name", "nexus")
    gmail = FakeGmail({"grab": [{"message_id": "g1"}]}, {"g1": EmailMessage("g1", "x", "grab")})

    counts, writer = await _run(gmail, ledger, notes, mapping={"g1": []})

    assert counts["recorded"] == 0
    assert gmail.searched == []            # gate returned before any work
    assert writer.calls == []


@pytest.mark.asyncio
async def test_already_processed_is_not_refetched(ledger, notes):
    ledger.record(message_id="g1", source="grab", status="archived",
                  amount_idr=41500, expense_date="2026-07-05")
    gmail = FakeGmail({"grab": [{"message_id": "g1"}]}, {"g1": EmailMessage("g1", "x", "grab")})

    counts, writer = await _run(gmail, ledger, notes, mapping={"g1": [ExtractedExpense("x", 1, "IDR", "Food", 0.9)]})

    assert counts["emails"] == 0
    assert writer.calls == []
    assert gmail.archived == []
