"""Chat tools to resolve expenses the email pipeline left pending.

When the daily email-expenses run cannot confidently categorise a charge, it
parks it in the ledger's pending queue and posts the list to the finance topic.
These tools let the supervisor act on the user's reply from chat:

  * ``list_pending_expenses()``              — show what is waiting
  * ``resolve_pending_expense(id, category)`` — write it to Notion + archive its email
  * ``skip_pending_expense(id)``            — drop it (not a real expense)

Resolution reuses the canonical ``add_expense`` (FIFO IDR→RUB→USD) and then
archives the source email and records it in the ledger, so a chat-resolved
expense ends up identical to an auto one — and the next cron run skips it.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from kronos.cron.expenses.gmail import archiving_enabled, get_gmail_client
from kronos.cron.expenses.ledger import get_ledger
from kronos.tools.expense import VALID_CATEGORIES, add_expense

log = logging.getLogger("kronos.tools.expense_pending")

SUPPORTED_CURRENCIES = ("IDR", "RUB", "USD")


def _format_row(row) -> str:
    amount = row["amount"]
    amount_str = f"{amount:,.0f}" if amount is not None else "?"
    guess = row["guessed_category"] or "?"
    return (
        f"#{row['id']} [{row['source']}] {amount_str} {row['currency'] or ''} "
        f"— {row['description']} (предположительно: {guess}; {row['reason']})"
    )


@tool
def list_pending_expenses() -> str:
    """List expenses from email that are waiting for a category decision.

    Call when the user asks about pending/unclear expenses, or before resolving
    one, to see the ids. Each line shows the pending id to pass to
    resolve_pending_expense / skip_pending_expense.
    """
    rows = get_ledger().list_pending()
    if not rows:
        return "Нет расходов, ожидающих категоризации."
    lines = [f"Ожидают категории ({len(rows)}):"]
    lines.extend(f"  {_format_row(row)}" for row in rows)
    lines.append("\nЧтобы провести: resolve_pending_expense(id, category). Пропустить: skip_pending_expense(id).")
    return "\n".join(lines)


@tool
async def resolve_pending_expense(pending_id: int, category: str) -> str:
    """Record a pending email expense in Notion with the chosen category.

    Writes via the canonical add_expense (auto IDR→RUB→USD), then archives the
    source email and marks it processed so the next cron run skips it.

    Args:
        pending_id: Id from list_pending_expenses.
        category: One of Food, Transport, Subscriptions, Shopping, Travel, Health, Entertainment, Other.
    """
    ledger = get_ledger()
    row = ledger.get_pending(pending_id)
    if row is None or row["status"] != "pending":
        return f"[ERROR] Pending трата #{pending_id} не найдена или уже обработана."
    if category not in VALID_CATEGORIES:
        return f"[ERROR] Категория '{category}' недопустима. Допустимые: {', '.join(sorted(VALID_CATEGORIES))}"
    if row["currency"] not in SUPPORTED_CURRENCIES:
        return f"[ERROR] Валюта {row['currency']} не поддерживается для записи."

    result = str(
        add_expense.invoke(
            {
                "description": row["description"],
                "amount": row["amount"],
                "currency": row["currency"],
                "category": category,
                "date": row["expense_date"],
                "ref": row["message_id"],
            }
        )
    )
    if result.startswith("[ERROR]"):
        return f"[ERROR] Не удалось записать трату #{pending_id}: {result}"

    ledger.resolve_pending(pending_id, category)

    # Mark it processed so re-runs skip it. Archiving is opt-in (safety): only
    # remove it from the inbox when EMAIL_EXPENSES_ARCHIVE is enabled.
    archived = False
    message_id = row["message_id"]
    if message_id:
        if archiving_enabled():
            gmail = get_gmail_client()
            if gmail is not None:
                try:
                    archived = await gmail.archive(message_id)
                except Exception as e:
                    log.warning("Archive after resolve failed for %s: %s", message_id, e)
        ledger.record(
            message_id=message_id,
            source=row["source"],
            status="archived" if archived else "recorded",
            amount=row["amount"],
            currency=row["currency"],
            amount_idr=row["amount_idr"],
            expense_date=row["expense_date"],
            description=row["description"],
            category=category,
            archived=archived,
        )

    tail = " Письмо в архиве." if archived else " (письмо оставлено в инбоксе)"
    return f"✅ Трата #{pending_id} записана как {category}.{tail}\n{result}"


@tool
def skip_pending_expense(pending_id: int) -> str:
    """Discard a pending email expense that is not a real expense.

    Marks the source email processed (skipped) so it is not reprocessed. Use
    when the user says a pending item is a top-up, transfer or otherwise not a spend.

    Args:
        pending_id: Id from list_pending_expenses.
    """
    ledger = get_ledger()
    row = ledger.get_pending(pending_id)
    if row is None or row["status"] != "pending":
        return f"[ERROR] Pending трата #{pending_id} не найдена или уже обработана."
    ledger.discard_pending(pending_id)
    if row["message_id"]:
        ledger.record(message_id=row["message_id"], source=row["source"], status="skipped")
    return f"⏭ Трата #{pending_id} пропущена (не расход)."
