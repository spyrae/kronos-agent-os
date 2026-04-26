"""Auto Expenses from Gmail — parse receipts from email.

Pipeline: Gmail search (receipts/invoices) → LLM extract → Notion Expenses DB.
Runs daily at 08:00 UTC+8 (00:00 UTC).

Uses Google Workspace MCP for Gmail access (requires OAuth setup).
"""

import json
import logging
import os
import urllib.request
from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.notify import TOPIC_GENERAL, send_bot_api
from kronos.llm import ModelTier, get_model

log = logging.getLogger("kronos.cron.email_expenses")

GMAIL_ACCOUNT = os.environ.get("GMAIL_ACCOUNT", "")
EXPENSES_DB_ID = os.environ.get("NOTION_EXPENSES_DB_ID", "")

EXTRACT_PROMPT = """Extract expense information from these email snippets.

Emails:
{emails}

For each expense found, output JSON:
{{
  "expenses": [
    {{
      "description": "What was purchased",
      "amount": 123.45,
      "currency": "IDR|USD|MYR",
      "category": "Food|Transport|Shopping|Services|Subscription|Health|Entertainment|Other",
      "date": "2026-03-25",
      "source": "Which email/service"
    }}
  ]
}}

Rules:
- Only extract actual expenses (not marketing emails)
- If amount is unclear, skip
- Use the most specific category
- Date should be the transaction date, not email date
- Return empty array if no expenses found
"""


async def run_email_expenses() -> None:
    """Scan Gmail for receipts and create Notion expenses. Kronos only."""
    if settings.agent_name != "kronos":
        return

    if not settings.notion_api_key:
        log.info("NOTION_API_KEY not set, skipping email expenses")
        return

    # Search Gmail for receipts via Brave (simulating — real impl would use MCP)
    # In production, this would call Google Workspace MCP:
    # search_gmail_messages(user_google_email=GMAIL_ACCOUNT, query="receipt OR invoice newer_than:1d")
    emails = await _search_gmail_receipts()

    if not emails:
        log.info("No receipt emails found")
        return

    # Extract expenses via LLM
    expenses = await _extract_expenses(emails)

    if not expenses:
        log.info("No expenses extracted from emails")
        return

    # Save to Notion
    created = 0
    for expense in expenses:
        success = _create_notion_expense(expense)
        if success:
            created += 1

    if created:
        log.info("Created %d expenses from email receipts", created)
        send_bot_api(f"📧 Email Expenses: {created} новых расходов из почты", topic_id=TOPIC_GENERAL)


async def _search_gmail_receipts() -> list[str]:
    """Search Gmail for receipt/invoice emails.

    Uses Google Workspace MCP tool if available,
    otherwise returns empty (feature requires MCP setup).
    """
    # This is a placeholder — actual implementation will use
    # the Google Workspace MCP tool at runtime through the graph.
    # For cron, we need direct API access.

    if not settings.google_oauth_client_id:
        log.info("Google OAuth not configured, skipping Gmail search")
        return []

    # TODO: Implement direct Gmail API call or use MCP stdio
    # For now, this is a stub that the agent can call manually
    log.info("Gmail expense scanning requires MCP integration (use /expenses scan)")
    return []


async def _extract_expenses(emails: list[str]) -> list[dict]:
    """Extract expense data from email text via LLM."""
    emails_text = "\n\n---\n\n".join(emails[:10])

    prompt = EXTRACT_PROMPT.format(emails=emails_text[:4000])

    model = get_model(ModelTier.LITE)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    import re
    match = re.search(r'\{[\s\S]*\}', reply)
    if match:
        try:
            data = json.loads(match.group())
            return data.get("expenses", [])
        except json.JSONDecodeError:
            pass

    return []


def _create_notion_expense(expense: dict) -> bool:
    """Create an expense page in Notion."""
    token = settings.notion_api_key
    if not token:
        return False

    try:
        payload = json.dumps({
            "parent": {"database_id": EXPENSES_DB_ID},
            "properties": {
                "Name": {"title": [{"text": {"content": expense.get("description", "Email expense")}}]},
                "Amount": {"number": expense.get("amount", 0)},
                "Category": {"select": {"name": expense.get("category", "Other")}},
                "Date": {"date": {"start": expense.get("date", datetime.now(UTC).strftime("%Y-%m-%d"))}},
                "Source": {"select": {"name": "Email"}},
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status == 200

    except Exception as e:
        log.error("Notion expense creation failed: %s", e)
        return False
