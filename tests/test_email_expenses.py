import pytest

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


def test_email_expense_allows_rub(monkeypatch):
    fake_tool = _FakeAddExpense()
    monkeypatch.setattr(settings, "notion_api_key", "ntn_test")
    monkeypatch.setattr(email_expenses, "add_expense", fake_tool)

    ok = email_expenses._create_notion_expense(
        {
            "description": "Hosting",
            "amount": 496,
            "currency": "RUB",
            "category": "Subscription",
            "date": "2026-06-10",
        }
    )

    assert ok
    assert fake_tool.args == {
        "description": "Hosting",
        "amount": 496.0,
        "currency": "RUB",
        "category": "Subscriptions",
        "date": "2026-06-10",
        "ref": None,
    }


class _FakeMcpTool:
    def __init__(self, name, response):
        self.name = name
        self.description = f"{name} tool"
        self.response = response
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.response


@pytest.mark.asyncio
async def test_search_gmail_receipts_uses_google_workspace_search(monkeypatch):
    tool = _FakeMcpTool(
        "search_gmail_messages",
        {
            "messages": [
                {
                    "id": "msg-1",
                    "from": "Grab",
                    "date": "2026-06-10",
                    "subject": "Your receipt",
                    "snippet": "Paid 41,500 IDR",
                }
            ]
        },
    )
    monkeypatch.setattr(email_expenses, "_load_google_workspace_tools", lambda: _async_value([tool]))

    emails = await email_expenses._search_gmail_receipts()

    assert len(emails) == 1
    assert "Subject: Your receipt" in emails[0]
    assert "Paid 41,500 IDR" in emails[0]
    assert tool.calls[0]["query"] == email_expenses.GMAIL_RECEIPT_QUERY


@pytest.mark.asyncio
async def test_search_gmail_receipts_reads_message_ids_when_search_has_no_snippet(monkeypatch):
    search = _FakeMcpTool("search_gmail_messages", {"messages": [{"id": "abc123456"}]})
    read = _FakeMcpTool(
        "get_gmail_message",
        {
            "id": "abc123456",
            "subject": "Invoice",
            "body": "Amount: 900 RUB",
        },
    )
    monkeypatch.setattr(email_expenses, "_load_google_workspace_tools", lambda: _async_value([search, read]))

    emails = await email_expenses._search_gmail_receipts()

    assert emails == ["Message ID: abc123456\nSubject: Invoice\nAmount: 900 RUB"]
    assert read.calls == [{"message_id": "abc123456"}]


@pytest.mark.asyncio
async def test_run_email_expenses_orchestrates_search_extract_create_notify(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "notion_api_key", "ntn_test")
    monkeypatch.setenv("NOTION_EXPENSES_DB_ID", "db-test")
    created = []
    notifications = []

    async def fake_search():
        return ["Receipt: 496 RUB for hosting"]

    async def fake_extract(emails):
        assert emails == ["Receipt: 496 RUB for hosting"]
        return [
            {
                "description": "Hosting",
                "amount": 496,
                "currency": "RUB",
                "category": "Subscription",
                "date": "2026-06-10",
                "source": "email-123",
            }
        ]

    def fake_create(expense):
        created.append(expense)
        return True

    def fake_notify(message, **kwargs):
        notifications.append((message, kwargs))

    count = await email_expenses.run_email_expenses(
        email_searcher=fake_search,
        expense_extractor=fake_extract,
        expense_creator=fake_create,
        notifier=fake_notify,
    )

    assert count == 1
    assert created[0]["description"] == "Hosting"
    assert notifications == [("📧 Email Expenses: 1 новых расходов из почты", {"topic_id": email_expenses.TOPIC_GENERAL})]


@pytest.mark.asyncio
async def test_run_email_expenses_skips_without_expenses_db(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "notion_api_key", "ntn_test")
    monkeypatch.delenv("NOTION_EXPENSES_DB_ID", raising=False)

    async def fail_search():
        raise AssertionError("email search should not run without expenses DB")

    assert await email_expenses.run_email_expenses(email_searcher=fail_search) == 0


async def _async_value(value):
    return value
