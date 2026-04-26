"""Weekly Expense Digest — automated spending report.

Pipeline: Notion Expenses DB → LLM analysis → Telegram.
Runs weekly Sunday at 10:00 UTC+8 (02:00 UTC).
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone, timedelta

from kronos.config import settings
from kronos.cron.notify import send_bot_api, TOPIC_DIGEST
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.cron.expense_digest")
EXPENSES_DB_ID = os.environ.get("NOTION_EXPENSES_DB_ID", "")


def _query_notion_expenses(days: int = 7) -> list[dict]:
    """Query Notion Expenses DB for recent entries."""
    token = settings.notion_api_key
    if not token:
        log.warning("NOTION_API_KEY not set, skipping expense digest")
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    payload = json.dumps({
        "filter": {
            "property": "Date",
            "date": {"on_or_after": cutoff},
        },
        "sorts": [{"property": "Date", "direction": "descending"}],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{EXPENSES_DB_ID}/query",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        expenses = []
        for page in data.get("results", []):
            props = page.get("properties", {})
            expense = {}
            for key, val in props.items():
                pt = val.get("type", "")
                if pt == "title":
                    expense[key] = "".join(t.get("plain_text", "") for t in val.get("title", []))
                elif pt == "number":
                    expense[key] = val.get("number", 0)
                elif pt == "select":
                    s = val.get("select")
                    expense[key] = s.get("name", "") if s else ""
                elif pt == "date":
                    d = val.get("date")
                    expense[key] = d.get("start", "") if d else ""
            if expense:
                expenses.append(expense)

        return expenses
    except Exception as e:
        log.error("Notion expenses query failed: %s", e)
        return []


async def run_expense_digest() -> None:
    """Generate weekly expense report. Kronos only."""
    if settings.agent_name != "kronos":
        return

    expenses = _query_notion_expenses(days=7)
    if not expenses:
        log.info("No expenses found, skipping digest")
        return

    expenses_text = json.dumps(expenses, ensure_ascii=False, indent=2)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""Ты — финансовый аналитик. Дата: {today}.

Расходы за последнюю неделю:
{expenses_text[:4000]}

Создай еженедельный отчёт:
1. Итого потрачено (общая сумма)
2. По категориям (таблица: категория, сумма, % от общего)
3. Топ-3 крупнейших трат
4. Тренд по сравнению с обычным (если можно оценить)
5. Рекомендация (1-2 предложения)

Формат: HTML (<b>, <i>). Русский язык. Краткий, не более 1000 символов.
"""

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    digest = response.content if isinstance(response.content, str) else str(response.content)

    if not digest or len(digest) < 50:
        return

    log.info("Expense digest: %d chars, %d expenses", len(digest), len(expenses))
    send_bot_api(f"<b>💰 Expense Digest — {today}</b>\n\n{digest}", parse_mode="HTML", topic_id=TOPIC_DIGEST)
