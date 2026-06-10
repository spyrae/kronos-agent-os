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


def test_notion_create_page_requires_expenses_database_id(monkeypatch):
    monkeypatch.setattr(expense.settings, "notion_api_key", "secret")
    monkeypatch.delenv("NOTION_EXPENSES_DB_ID", raising=False)

    with pytest.raises(RuntimeError, match="NOTION_EXPENSES_DB_ID not configured"):
        expense._notion_create_page({})
