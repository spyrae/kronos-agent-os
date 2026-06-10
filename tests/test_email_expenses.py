from kronos.config import settings
from kronos.cron import email_expenses


class _FakeAddExpense:
    def __init__(self):
        self.args = None

    def invoke(self, args):
        self.args = args
        return "✅ ok"


def test_email_expense_uses_canonical_add_expense(monkeypatch):
    fake_tool = _FakeAddExpense()
    monkeypatch.setattr(settings, "notion_api_key", "ntn_test")
    monkeypatch.setattr(email_expenses, "add_expense", fake_tool)

    ok = email_expenses._create_notion_expense(
        {
            "description": "Receipt",
            "amount": "411500",
            "currency": "IDR",
            "category": "Services",
            "date": "2026-06-10",
            "source": "email-123",
        }
    )

    assert ok
    assert fake_tool.args == {
        "description": "Receipt",
        "amount": 411500.0,
        "currency": "IDR",
        "category": "Other",
        "date": "2026-06-10",
        "ref": "email-123",
    }
