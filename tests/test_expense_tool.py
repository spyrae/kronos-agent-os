import pytest

from kronos.tools import expense


def _budget_text(remaining: int = 5_000_000) -> str:
    """New 7-column format with both IDR/RUB and IDR/USD rates."""
    return f"""# Budget

## Активные транши

| # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Курс (IDR/USD) | Заметка |
|---|------|-----------|-------------|-----------------|-----------------|---------|
| 1 | 01.06.2026 | 5,000,000 | {remaining:,} | 233.5 | 16300 | Test |

## История
"""


def _budget_text_legacy(remaining: int = 1000) -> str:
    """Old 6-column format (no IDR/USD rate) — backward compatibility."""
    return f"""# Budget

## Активные транши

| # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Заметка |
|---|------|-----------|-------------|-----------------|---------|
| 1 | 01.06.2026 | 1,000 | {remaining:,} | 200 | Test |

## История
"""


def test_add_expense_does_not_update_budget_when_notion_fails(monkeypatch, tmp_path):
    budget_file = tmp_path / "BUDGET.md"
    original_budget = _budget_text()
    budget_file.write_text(original_budget)

    monkeypatch.setattr(expense, "_budget_path", lambda: str(budget_file))
    monkeypatch.setattr(
        expense,
        "_notion_create_page",
        lambda properties: (_ for _ in ()).throw(RuntimeError("Notion failed")),
    )

    result = expense.add_expense.invoke(
        {
            "description": "Кафе",
            "amount": 411500,
            "currency": "IDR",
            "category": "Food",
        }
    )

    assert result == "[ERROR] Failed to write to Notion: Notion failed"
    assert budget_file.read_text() == original_budget


def test_add_expense_updates_budget_after_notion_success(monkeypatch, tmp_path):
    budget_file = tmp_path / "BUDGET.md"
    budget_file.write_text(_budget_text())
    captured_properties = {}

    monkeypatch.setattr(expense, "_budget_path", lambda: str(budget_file))
    monkeypatch.setattr(expense, "_schedule_duplicate_cleanup", lambda **kwargs: None)

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "Кафе",
            "amount": 411500,
            "currency": "IDR",
            "category": "Food",
        }
    )

    # IDR converts to BOTH RUB and USD from the tranche.
    assert "✅ 'Кафе' — 411,500 IDR" in result
    assert "= 1,762 ₽ / 25.25 $" in result
    assert "| Остаток: 4,588,500 IDR" in result
    assert captured_properties["Amount_RUB"] == {"number": 1762}
    assert captured_properties["Amount_USD"] == {"number": 25.25}
    assert captured_properties["Rate"] == {"number": 233.5}
    assert captured_properties["Rate_USD"] == {"number": 16300.0}
    assert "| 1 | 01.06.2026 | 5,000,000 | 4,588,500 | 233.5 | 16300 | Test |" in budget_file.read_text()


def test_add_expense_idr_legacy_tranche_yields_no_usd(monkeypatch, tmp_path):
    """A legacy 6-column tranche still converts to RUB, but not USD."""
    budget_file = tmp_path / "BUDGET.md"
    budget_file.write_text(_budget_text_legacy())
    captured_properties = {}

    monkeypatch.setattr(expense, "_budget_path", lambda: str(budget_file))
    monkeypatch.setattr(expense, "_schedule_duplicate_cleanup", lambda **kwargs: None)

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "Кафе",
            "amount": 200,
            "currency": "IDR",
            "category": "Food",
        }
    )

    assert "✅ 'Кафе' — 200 IDR" in result
    assert captured_properties["Amount_RUB"] == {"number": 1}
    assert captured_properties["Rate"] == {"number": 200.0}
    assert "Amount_USD" not in captured_properties
    assert "Rate_USD" not in captured_properties
    # Budget still parses and updates (legacy row rewritten with empty USD cell).
    assert "| 1 | 01.06.2026 | 1,000 | 800 |" in budget_file.read_text()


def test_add_expense_rub_writes_amount_rub_without_fifo(monkeypatch):
    captured_properties = {}

    monkeypatch.setattr(
        expense,
        "_budget_path",
        lambda: (_ for _ in ()).throw(AssertionError("RUB must not read budget")),
    )
    monkeypatch.setattr(
        expense,
        "_schedule_duplicate_cleanup",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("RUB must not run IDR duplicate cleanup")),
    )

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "JourneyBay (хостинг)",
            "amount": 496,
            "currency": "RUB",
            "category": "Subscriptions",
        }
    )

    assert "✅ 'JourneyBay (хостинг)' — 496 ₽" in result
    assert "Остаток" not in result
    assert captured_properties["Amount_RUB"] == {"number": 496}
    assert "Amount_IDR" not in captured_properties
    assert "Amount_USD" not in captured_properties
    assert "Rate" not in captured_properties
    assert "Rate_USD" not in captured_properties


def test_add_expense_usd_writes_amount_usd_without_fifo(monkeypatch):
    captured_properties = {}

    monkeypatch.setattr(
        expense,
        "_budget_path",
        lambda: (_ for _ in ()).throw(AssertionError("USD must not read budget")),
    )
    monkeypatch.setattr(
        expense,
        "_schedule_duplicate_cleanup",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("USD must not run IDR duplicate cleanup")),
    )

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "ChatGPT",
            "amount": 12.5,
            "currency": "USD",
            "category": "Subscriptions",
        }
    )

    assert "✅ 'ChatGPT' — 12.50 $" in result
    assert "Остаток" not in result
    assert captured_properties["Amount_USD"] == {"number": 12.5}
    assert "Amount_IDR" not in captured_properties
    assert "Amount_RUB" not in captured_properties
    assert "Rate" not in captured_properties
    assert "Rate_USD" not in captured_properties


def test_add_expense_split_full_halves_everything_for_idr(monkeypatch, tmp_path):
    """Maybank-style shared card: the WHOLE IDR charge is halved before recording."""
    budget_file = tmp_path / "BUDGET.md"
    budget_file.write_text(_budget_text())
    captured_properties = {}

    monkeypatch.setattr(expense, "_budget_path", lambda: str(budget_file))
    monkeypatch.setattr(expense, "_schedule_duplicate_cleanup", lambda **kwargs: None)

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "Ужин",
            "amount": 411500,
            "currency": "IDR",
            "category": "Food",
            "split_full": True,
        }
    )

    # amount, Amount_IDR, converted RUB/USD AND the budget deduction are all halved.
    assert "✅ 'Ужин' — 205,750 IDR" in result
    assert "(split, твоя доля)" in result
    assert captured_properties["Amount_IDR"] == {"number": 205750}
    assert captured_properties["Amount_RUB"] == {"number": 881}  # 205750 / 233.5
    assert captured_properties["Amount_USD"] == {"number": 12.62}  # 205750 / 16300
    assert captured_properties["Split"] == {"checkbox": True}
    # Budget deducts the halved amount, not the full charge.
    assert "| 1 | 01.06.2026 | 5,000,000 | 4,794,250 | 233.5 | 16300 | Test |" in budget_file.read_text()


def test_add_expense_split_keeps_idr_whole_and_halves_only_share(monkeypatch, tmp_path):
    """Regression: legacy `split` keeps Amount_IDR whole, halving only RUB/USD."""
    budget_file = tmp_path / "BUDGET.md"
    budget_file.write_text(_budget_text())
    captured_properties = {}

    monkeypatch.setattr(expense, "_budget_path", lambda: str(budget_file))
    monkeypatch.setattr(expense, "_schedule_duplicate_cleanup", lambda **kwargs: None)

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "Кафе",
            "amount": 411500,
            "currency": "IDR",
            "category": "Food",
            "split": True,
        }
    )

    assert "✅ 'Кафе' — 411,500 IDR" in result  # full charge shown
    assert captured_properties["Amount_IDR"] == {"number": 411500}  # NOT halved
    assert captured_properties["Amount_RUB"] == {"number": 881}  # 1762 / 2
    assert captured_properties["Amount_USD"] == {"number": 12.62}  # 25.25 / 2
    assert captured_properties["Split"] == {"checkbox": True}
    # The full charge still leaves the shared IDR budget.
    assert "| 1 | 01.06.2026 | 5,000,000 | 4,588,500 | 233.5 | 16300 | Test |" in budget_file.read_text()


def test_add_expense_split_full_halves_rub_without_fifo(monkeypatch):
    captured_properties = {}

    monkeypatch.setattr(
        expense,
        "_budget_path",
        lambda: (_ for _ in ()).throw(AssertionError("RUB must not read budget")),
    )

    def fake_notion_create_page(properties):
        captured_properties.update(properties)
        return {"id": "page-id"}

    monkeypatch.setattr(expense, "_notion_create_page", fake_notion_create_page)

    result = expense.add_expense.invoke(
        {
            "description": "Подписка",
            "amount": 496,
            "currency": "RUB",
            "category": "Subscriptions",
            "split_full": True,
        }
    )

    assert "✅ 'Подписка' — 248 ₽" in result
    assert captured_properties["Amount_RUB"] == {"number": 248}  # 496 / 2
    assert captured_properties["Split"] == {"checkbox": True}


def test_add_expense_rejects_unknown_currency(monkeypatch):
    monkeypatch.setattr(
        expense,
        "_notion_create_page",
        lambda properties: (_ for _ in ()).throw(AssertionError("must not reach Notion")),
    )

    result = expense.add_expense.invoke(
        {
            "description": "Something",
            "amount": 10,
            "currency": "EUR",
            "category": "Other",
        }
    )

    assert result.startswith("[ERROR] Invalid currency 'EUR'")


def test_cleanup_archives_only_incomplete_duplicate(monkeypatch):
    def page(page_id, title, amount_rub, rate):
        return {
            "id": page_id,
            "properties": {
                "Description": {"title": [{"plain_text": title}]},
                "Amount_RUB": {"number": amount_rub},
                "Rate": {"number": rate},
            },
        }

    monkeypatch.setattr(
        expense,
        "_query_duplicate_candidates",
        lambda date, amount_idr: [
            page("keep", "путешествия, жилье", 9078, 233.5),
            page("bad", "Путешествия, жилье", None, None),
            page("complete", "Путешествия, жилье", 9078, 233.5),
            page("other-title", "путешествия", None, None),
        ],
    )
    archived = []
    monkeypatch.setattr(expense, "_archive_page", archived.append)

    count = expense._cleanup_incomplete_duplicates(
        description="путешествия, жилье",
        amount_idr=2_120_000,
        date="2026-06-10",
        keep_page_id="keep",
    )

    assert count == 1
    assert archived == ["bad"]


def test_fifo_calculates_rub_and_usd():
    tranches = [
        {
            "num": 1,
            "date": "01.06.2026",
            "total": 1_000_000,
            "remaining": 1_000_000,
            "rate": 233.5,
            "rate_usd": 16300,
            "note": "Test",
        }
    ]

    result = expense._fifo_calculate(411_500, tranches)

    assert result.amount_rub == 1762
    assert expense._notion_rate(result.rate_rub) == 233.5
    assert result.amount_usd == round(411_500 / 16_300, 2)
    assert expense._notion_rate_usd(result.rate_usd) == 16300.0


def test_fifo_legacy_tranche_yields_no_usd():
    tranches = [
        {
            "num": 1,
            "date": "01.06.2026",
            "total": 1_000_000,
            "remaining": 1_000_000,
            "rate": 233.5,
            "rate_usd": None,
            "note": "Test",
        }
    ]

    result = expense._fifo_calculate(411_500, tranches)

    assert result.amount_rub == 1762
    assert result.amount_usd is None
    assert result.rate_usd is None


def test_parse_tranches_reads_both_rates():
    tranches = expense._parse_tranches(_budget_text())
    assert len(tranches) == 1
    assert tranches[0]["rate"] == 233.5
    assert tranches[0]["rate_usd"] == 16300.0
    assert tranches[0]["note"] == "Test"


def test_parse_tranches_legacy_note_not_mistaken_for_usd_rate():
    tranches = expense._parse_tranches(_budget_text_legacy())
    assert len(tranches) == 1
    assert tranches[0]["rate"] == 200.0
    assert tranches[0]["rate_usd"] is None
    assert tranches[0]["note"] == "Test"


def test_notion_create_page_requires_expenses_database_id(monkeypatch):
    monkeypatch.setattr(expense.settings, "notion_api_key", "secret")
    monkeypatch.delenv("NOTION_EXPENSES_DB_ID", raising=False)

    with pytest.raises(RuntimeError, match="NOTION_EXPENSES_DB_ID not configured"):
        expense._notion_create_page({})
