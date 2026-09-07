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
from kronos.cron.expenses.processor import _is_split_source
from kronos.security.untrusted import mark_untrusted
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

    if not ledger.claim_pending(pending_id):
        return f"[ERROR] Трата #{pending_id} уже обрабатывается."
    try:
        result = str(
            add_expense.invoke(
                {
                    "description": row["description"],
                    "amount": row["amount"],
                    "currency": row["currency"],
                    "category": category,
                    "date": row["expense_date"],
                    "split_full": _is_split_source(row["source"]),
                    "ref": ledger.pending_reference(pending_id),
                }
            )
        )
    except Exception as exc:
        log.warning("Pending write outcome unknown (%s)", type(exc).__name__)
        result = "[ERROR] Failed to write to Notion: outcome unknown"
    if not result.startswith("✅"):
        uncertain = not result.startswith("[ERROR]") or result.startswith("[ERROR] Failed to write to Notion:")
        ledger.release_pending_claim(pending_id, uncertain=uncertain)
        if row["message_id"]:
            ledger.finalize_message(row["message_id"], row["source"])
        if uncertain:
            return f"[ERROR] Результат записи траты #{pending_id} неизвестен; нужна сверка, повтор остановлен."
        return f"[ERROR] Не удалось записать трату #{pending_id}: {result}"

    ledger.resolve_pending(pending_id, category)

    # Resolving one charge must not consume its siblings. Derive the email
    # state first, and archive only when every automatic/manual item is done.
    archived = False
    message_id = row["message_id"]
    if message_id:
        status = ledger.finalize_message(message_id, row["source"])
        if status in {"recorded", "duplicate"} and archiving_enabled():
            gmail = get_gmail_client()
            if gmail is not None:
                try:
                    archived = await gmail.archive(message_id)
                except Exception as exc:
                    log.warning("Archive after resolve failed (%s)", type(exc).__name__)
                if archived:
                    ledger.mark_archived(message_id)

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
    if not ledger.discard_pending(pending_id):
        return f"[ERROR] Трата #{pending_id} уже обрабатывается."
    if row["message_id"]:
        ledger.finalize_message(row["message_id"], row["source"])
    return f"⏭ Трата #{pending_id} пропущена (не расход)."


# Pending rows are built from parsed email — merchant names and descriptions come
# from whoever sent the receipt, so the listing is external content.
mark_untrusted([list_pending_expenses], reason="email-derived expenses")
