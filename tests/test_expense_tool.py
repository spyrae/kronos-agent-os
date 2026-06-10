import pytest

from kronos.tools import expense


def _budget_text(remaining: int = 1000) -> str:
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
            "amount": 200,
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
            "amount": 200,
            "currency": "IDR",
            "category": "Food",
        }
    )

    assert "✅ 'Кафе' — 200 IDR = 1 ₽" in result
    assert "| Остаток: 800 IDR" in result
    assert captured_properties["Amount_RUB"] == {"number": 1}
    assert captured_properties["Rate"] == {"number": 200.0}
    assert "| 1 | 01.06.2026 | 1,000 | 800 | 200.0 | Test |" in budget_file.read_text()


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
    assert "Rate" not in captured_properties


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


def test_notion_rate_stays_idr_per_rub():
    tranches = [
        {
            "num": 1,
            "date": "01.06.2026",
            "total": 1_000_000,
            "remaining": 1_000_000,
            "rate": 233.5,
            "note": "Test",
        }
    ]

    amount_rub, effective_rate, _ = expense._fifo_calculate(411_500, tranches)

    assert amount_rub == 1762
    assert expense._notion_rate(effective_rate) == 233.5


def test_notion_create_page_requires_expenses_database_id(monkeypatch):
    monkeypatch.setattr(expense.settings, "notion_api_key", "secret")
    monkeypatch.delenv("NOTION_EXPENSES_DB_ID", raising=False)

    with pytest.raises(RuntimeError, match="NOTION_EXPENSES_DB_ID not configured"):
        expense._notion_create_page({})
