import pytest

from kronos.cron.expenses.ledger import ExpenseLedger
from kronos.db import SafeDB
from kronos.tools import expense_pending as ep


class FakeAddExpense:
    def __init__(self, result="✅ ok"):
        self.result = result
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        return self.result


class FakeGmail:
    def __init__(self, ok=True):
        self.ok = ok
        self.archived = []

    async def archive(self, message_id):
        self.archived.append(message_id)
        return self.ok


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    led = ExpenseLedger(SafeDB(tmp_path / "expenses_ledger.db"))
    monkeypatch.setattr(ep, "get_ledger", lambda: led)
    return led


@pytest.fixture
def add_expense(monkeypatch):
    fake = FakeAddExpense()
    monkeypatch.setattr(ep, "add_expense", fake)
    return fake


@pytest.fixture
def gmail(monkeypatch):
    fake = FakeGmail()
    monkeypatch.setattr(ep, "get_gmail_client", lambda: fake)
    return fake


def _seed(ledger) -> int:
    return ledger.add_pending(
        message_id="p1", source="permata", description="ATM Bali",
        amount=500000, currency="IDR", amount_idr=500000,
        expense_date="2026-07-05", guessed_category="Other", reason="low confidence",
    )


def _seed_maybank(ledger) -> int:
    return ledger.add_pending(
        message_id="m1", source="maybank", description="Resto",
        amount=300000, currency="IDR", amount_idr=300000,
        expense_date="2026-07-05", guessed_category="Food", reason="low confidence",
    )


def test_list_empty(ledger):
    assert "Нет расходов" in ep.list_pending_expenses.invoke({})


def test_list_shows_rows(ledger):
    pid = _seed(ledger)
    out = ep.list_pending_expenses.invoke({})
    assert f"#{pid}" in out
    assert "ATM Bali" in out


@pytest.mark.asyncio
async def test_resolve_writes_archives_and_marks_processed(ledger, add_expense, gmail, monkeypatch):
    monkeypatch.setenv("EMAIL_EXPENSES_ARCHIVE", "true")
    pid = _seed(ledger)

    res = await ep.resolve_pending_expense.ainvoke({"pending_id": pid, "category": "Health"})

    assert res.startswith("✅")
    assert add_expense.calls[0]["category"] == "Health"
    assert add_expense.calls[0]["ref"] == "p1"
    assert add_expense.calls[0]["split_full"] is False   # permata is not a split source
    assert ledger.get_pending(pid)["status"] == "resolved"
    assert gmail.archived == ["p1"]
    assert ledger.is_processed("p1") is True   # so the next cron run skips it


@pytest.mark.asyncio
async def test_resolve_maybank_keeps_split_full(ledger, add_expense, gmail):
    """A Maybank pending resolved from chat must still be halved (split_full)."""
    pid = _seed_maybank(ledger)

    res = await ep.resolve_pending_expense.ainvoke({"pending_id": pid, "category": "Food"})

    assert res.startswith("✅")
    assert add_expense.calls[0]["split_full"] is True
    assert ledger.get_pending(pid)["status"] == "resolved"


@pytest.mark.asyncio
async def test_resolve_without_archiving_records_but_keeps_email(ledger, add_expense, gmail, monkeypatch):
    monkeypatch.delenv("EMAIL_EXPENSES_ARCHIVE", raising=False)  # default OFF
    pid = _seed(ledger)

    res = await ep.resolve_pending_expense.ainvoke({"pending_id": pid, "category": "Health"})

    assert res.startswith("✅")
    assert "инбоксе" in res
    assert gmail.archived == []                 # email left in inbox
    assert ledger.get_pending(pid)["status"] == "resolved"
    assert ledger.is_processed("p1") is True    # recorded → next run skips it


@pytest.mark.asyncio
async def test_resolve_rejects_invalid_category(ledger, add_expense, gmail):
    pid = _seed(ledger)
    res = await ep.resolve_pending_expense.ainvoke({"pending_id": pid, "category": "Bogus"})
    assert res.startswith("[ERROR]")
    assert add_expense.calls == []
    assert ledger.get_pending(pid)["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_unknown_id(ledger, add_expense, gmail):
    res = await ep.resolve_pending_expense.ainvoke({"pending_id": 999, "category": "Food"})
    assert res.startswith("[ERROR]")
    assert add_expense.calls == []


@pytest.mark.asyncio
async def test_resolve_write_failure_keeps_pending(ledger, gmail, monkeypatch):
    monkeypatch.setattr(ep, "add_expense", FakeAddExpense(result="[ERROR] notion down"))
    pid = _seed(ledger)

    res = await ep.resolve_pending_expense.ainvoke({"pending_id": pid, "category": "Health"})

    assert res.startswith("[ERROR]")
    assert ledger.get_pending(pid)["status"] == "pending"   # not lost
    assert gmail.archived == []                              # not archived


@pytest.mark.asyncio
async def test_resolve_without_gmail_still_records(ledger, add_expense, monkeypatch):
    monkeypatch.setattr(ep, "get_gmail_client", lambda: None)
    pid = _seed(ledger)

    res = await ep.resolve_pending_expense.ainvoke({"pending_id": pid, "category": "Health"})

    assert res.startswith("✅")
    assert "инбоксе" in res
    assert ledger.get_pending(pid)["status"] == "resolved"
    assert ledger.is_processed("p1") is True   # recorded even without archive


def test_skip_discards_and_marks_skipped(ledger):
    pid = _seed(ledger)
    res = ep.skip_pending_expense.invoke({"pending_id": pid})
    assert res.startswith("⏭")
    assert ledger.get_pending(pid)["status"] == "discarded"
    assert ledger.is_processed("p1") is True   # skipped → not reprocessed
