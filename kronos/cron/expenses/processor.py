"""Deterministic controller for the email-expenses run.

Python owns the control flow — idempotency, dedup, write-then-archive ordering,
and the pending queue — while the LLM is used only to extract and audit. This is
the reliability boundary: a hallucinated amount or a wrong category can send an
expense to the pending queue, but it cannot silently double-write or drop a
charge, because those decisions are made here against the ledger and the Notion
API, not by the model.

Per email::

    extract (LLM, one email)
      └─ for each expense:
           unsupported currency        → pending
           cross-source dup (amt+date) → duplicate (archive, no write)
           low category confidence     → pending
           audit fails (LLM, 2nd pass) → pending
           else                        → add_expense (FIFO IDR→RUB→USD)
      └─ if anything was recorded → archive email + mark processed
         if only duplicates       → archive email + mark duplicate
         if only pending          → leave email in inbox, keep it queued

Ordering: Grab is searched before the banks so that when both email about one
card charge, Grab's richer record lands first and the bank copy dedups against
it (the user's "dedup by amount+date, keep the more detailed one" choice).

Every run posts a report to the finance topic — always, even on empty runs —
listing how many emails were scanned, what was recorded (with amounts), what was
deduped/skipped, and what is waiting for a category. ``dry_run=True`` performs
extraction + audit but writes nothing and archives nothing: a safe way to see
what a real run would do against the live mailbox.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from kronos.config import settings
from kronos.cron.expenses.extract import (
    SUPPORTED_CURRENCIES,
    audit_expense,
    extract_expenses,
)
from kronos.cron.expenses.gmail import archiving_enabled, get_gmail_client
from kronos.cron.expenses.ledger import get_ledger
from kronos.cron.notify import TOPIC_FINANCE, send_bot_api
from kronos.tools.expense import USER_TZ

log = logging.getLogger("kronos.cron.expenses.processor")

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_SEARCH_LIMIT = 25


def _today() -> str:
    return datetime.now(USER_TZ).strftime("%Y-%m-%d")


def _threshold() -> float:
    try:
        return float(os.environ.get("EXPENSES_CATEGORY_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD))
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD


def _source_queries() -> list[tuple[str, str]]:
    """(source, Gmail query) pairs. Grab first for dedup detail-priority.

    Queries are overridable per source via env once exact sender addresses are
    known; the defaults are broad name/keyword matches (incl. Indonesian terms)
    so the pipeline works before any templates are collected.
    """
    try:
        lookback = int(os.environ.get("EMAIL_EXPENSES_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS))
    except ValueError:
        lookback = DEFAULT_LOOKBACK_DAYS
    win = f"newer_than:{lookback}d"
    # Sender-name matches (Gmail ``from:`` matches a substring of the address, so
    # these hit the real senders without hardcoding personal addresses). Set the
    # exact address via env per source if a name is too broad.
    grab = os.environ.get("EMAIL_EXPENSES_QUERY_GRAB", f"{win} from:grab")
    wondr = os.environ.get("EMAIL_EXPENSES_QUERY_WONDR", f"{win} from:wondr")
    permata = os.environ.get("EMAIL_EXPENSES_QUERY_PERMATA", f"{win} from:permata")
    return [("grab", grab), ("wondr", wondr), ("permata", permata)]


def _new_counts() -> dict[str, int]:
    return {
        "emails": 0,
        "recorded": 0,
        "duplicates": 0,
        "skipped": 0,
        "pending": 0,
        "archived": 0,
        "errors": 0,
    }


def _default_writer(payload: dict) -> str:
    from kronos.tools.expense import add_expense

    return str(add_expense.invoke(payload))


class _Report:
    """Collects human-readable lines for the per-run Telegram report."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.recorded: list[str] = []  # write result lines (real) / previews (dry)
        self.pending: list[str] = []  # queued-for-category lines
        self.sources: list[str] = []  # sources that returned mail

    def add_recorded(self, line: str) -> None:
        self.recorded.append(line)

    def add_pending(self, line: str) -> None:
        self.pending.append(line)


async def run_email_expenses(
    *,
    gmail_client=None,
    ledger=None,
    extractor=extract_expenses,
    auditor=audit_expense,
    expense_writer=_default_writer,
    notifier=send_bot_api,
    model=None,
    dry_run: bool = False,
) -> dict:
    """Scan Gmail for spend confirmations and record them in Notion. Kronos only."""
    if settings.agent_name != "kronos":
        return _new_counts()
    if not settings.notion_api_key or not os.environ.get("NOTION_EXPENSES_DB_ID", "").strip():
        log.info("Notion not configured — skipping email expenses")
        return _new_counts()

    gmail = gmail_client or get_gmail_client()
    if gmail is None:
        return _new_counts()
    ledger = ledger or get_ledger()

    counts = _new_counts()
    report = _Report(dry_run)
    threshold = _threshold()
    seen_dry: set[tuple] = set()  # in-run dedup for dry runs (ledger untouched)

    # 1) Collect candidate message refs across sources (Grab first).
    source_by_id: dict[str, str] = {}
    for source, query in _source_queries():
        refs = await gmail.search(query, limit=DEFAULT_SEARCH_LIMIT)
        if refs and source not in report.sources:
            report.sources.append(source)
        for ref in refs:
            mid = ref["message_id"]
            if mid and mid not in source_by_id:
                source_by_id[mid] = source

    # 2) Drop anything already handled or already queued as pending.
    todo = [mid for mid in source_by_id if not ledger.is_processed(mid) and not ledger.has_pending(mid)]

    # 3) Fetch full content and process each email deterministically.
    if todo:
        messages = await gmail.fetch(todo)
        for msg in messages:
            counts["emails"] += 1
            msg.source = source_by_id.get(msg.message_id, "other")
            await _process_email(
                msg,
                ledger=ledger,
                extractor=extractor,
                auditor=auditor,
                expense_writer=expense_writer,
                gmail=gmail,
                model=model,
                threshold=threshold,
                counts=counts,
                report=report,
                dry_run=dry_run,
                seen_dry=seen_dry,
            )

    # 4) Always post a report to the finance topic so the run is visible.
    #    A real run lists ALL open pending (with ids) so the agent re-asks the
    #    user every run until each is resolved from chat.
    open_pending_rows = [] if dry_run else ledger.list_pending()
    notifier(
        _format_report(counts, report, open_pending_rows, archiving_on=archiving_enabled()),
        topic_id=TOPIC_FINANCE,
    )

    log.info("Email expenses done (dry_run=%s): %s", dry_run, counts)
    return counts


async def _process_email(
    msg,
    *,
    ledger,
    extractor,
    auditor,
    expense_writer,
    gmail,
    model,
    threshold,
    counts,
    report,
    dry_run,
    seen_dry,
) -> None:
    expenses = extractor(msg, model=model)
    if not expenses:
        # Not a spend email (top-up, transfer, marketing). Handled, not archived.
        if not dry_run:
            ledger.record(message_id=msg.message_id, source=msg.source, status="skipped")
        counts["skipped"] += 1
        return

    outcomes: list[str] = []
    repr_amount_idr: float | None = None
    repr_date: str | None = None

    for exp in expenses:
        outcome, amount_idr, date = _handle_expense(
            msg,
            exp,
            ledger=ledger,
            auditor=auditor,
            expense_writer=expense_writer,
            model=model,
            threshold=threshold,
            counts=counts,
            report=report,
            dry_run=dry_run,
            seen_dry=seen_dry,
        )
        outcomes.append(outcome)
        if outcome == "recorded" and repr_amount_idr is None and amount_idr is not None:
            repr_amount_idr, repr_date = amount_idr, date

    if dry_run:
        return

    if "recorded" in outcomes:
        ledger.record(
            message_id=msg.message_id,
            source=msg.source,
            status="recorded",
            amount_idr=repr_amount_idr,
            expense_date=repr_date,
        )
        await _archive(msg, gmail=gmail, ledger=ledger, counts=counts)
    elif "error" in outcomes:
        # Leave non-terminal so the next run retries. Notion dedup guards writes.
        ledger.record(message_id=msg.message_id, source=msg.source, status="error", error="expense write failed")
    elif "pending" in outcomes:
        # Only unclear expenses — keep the email in the inbox until resolved.
        # has_pending() guards against re-queueing on the next run.
        return
    else:  # all duplicates
        ledger.record(message_id=msg.message_id, source=msg.source, status="duplicate")
        await _archive(msg, gmail=gmail, ledger=ledger, counts=counts)


def _handle_expense(
    msg,
    exp,
    *,
    ledger,
    auditor,
    expense_writer,
    model,
    threshold,
    counts,
    report,
    dry_run,
    seen_dry,
) -> tuple[str, float | None, str | None]:
    """Decide + act on one extracted expense. Returns (outcome, amount_idr, date)."""
    date = exp.expense_date or _today()
    amount_idr = exp.amount if exp.currency == "IDR" else None

    if exp.currency not in SUPPORTED_CURRENCIES:
        _queue_pending(
            ledger,
            msg,
            exp,
            amount_idr,
            date,
            reason=f"unsupported currency {exp.currency}",
            counts=counts,
            report=report,
            dry_run=dry_run,
        )
        return "pending", None, None

    dup_key = (amount_idr, date)
    is_dup = ledger.find_recorded_duplicate(amount_idr, date) is not None
    if dry_run and amount_idr is not None and dup_key in seen_dry:
        is_dup = True
    if is_dup:
        counts["duplicates"] += 1
        return "duplicate", None, None

    if exp.category is None or exp.confidence < threshold:
        _queue_pending(
            ledger,
            msg,
            exp,
            amount_idr,
            date,
            reason="low category confidence",
            counts=counts,
            report=report,
            dry_run=dry_run,
        )
        return "pending", None, None

    verdict = auditor(msg.text, exp, model=model)
    if not (verdict.ok and verdict.amount_matches and verdict.is_expense):
        _queue_pending(
            ledger,
            msg,
            exp,
            amount_idr,
            date,
            reason=f"audit rejected: {verdict.issues or 'unverified'}",
            counts=counts,
            report=report,
            dry_run=dry_run,
        )
        return "pending", None, None

    category = verdict.category or exp.category

    if dry_run:
        if amount_idr is not None:
            seen_dry.add(dup_key)
        report.add_recorded(
            f"🔎 [{msg.source}] {exp.amount:,.0f} {exp.currency} — {exp.description} "
            f"→ {category} (conf {exp.confidence:.0%}, audit ✓)"
        )
        counts["recorded"] += 1
        return "recorded", amount_idr, date

    result = expense_writer(
        {
            "description": exp.description,
            "amount": exp.amount,
            "currency": exp.currency,
            "category": category,
            "date": exp.expense_date,  # None → add_expense uses today
            "ref": msg.message_id,
        }
    )
    if result.startswith("[ERROR]"):
        log.error("add_expense failed for %s: %s", msg.message_id, result)
        counts["errors"] += 1
        return "error", None, None

    report.add_recorded(f"[{msg.source}] {result}")
    counts["recorded"] += 1
    return "recorded", amount_idr, date


def _queue_pending(ledger, msg, exp, amount_idr, date, *, reason, counts, report, dry_run) -> None:
    if not dry_run:
        ledger.add_pending(
            message_id=msg.message_id,
            source=msg.source,
            description=exp.description,
            amount=exp.amount,
            currency=exp.currency,
            amount_idr=amount_idr,
            expense_date=date,
            guessed_category=exp.category,
            reason=reason,
        )
    guess = exp.category or "?"
    report.add_pending(
        f"[{msg.source}] {exp.amount:,.0f} {exp.currency} — {exp.description} (предположительно: {guess}; {reason})"
    )
    counts["pending"] += 1


async def _archive(msg, *, gmail, ledger, counts) -> None:
    # Safety: archiving is opt-in. When off, the email stays in the inbox and the
    # ledger keeps it as 'recorded' (still processed → not re-recorded next run).
    if not archiving_enabled():
        return
    if await gmail.archive(msg.message_id):
        ledger.mark_archived(msg.message_id)
        counts["archived"] += 1


def _format_report(counts: dict[str, int], report: _Report, open_pending_rows, archiving_on: bool = False) -> str:
    tag = " [DRY-RUN — ничего не записано]" if report.dry_run else ""
    sources = ", ".join(report.sources) if report.sources else "—"
    archive_cell = f"🗄 В архив: {counts['archived']}" if archiving_on else "🗄 Архив: выкл"
    lines = [
        f"📧 <b>Расходы из почты</b>{tag}",
        f"Просканировано писем: {counts['emails']} | Источники: {sources}",
        (
            f"✅ Записано: {counts['recorded']} | 🔁 Дублей: {counts['duplicates']} | "
            f"⏭ Пропущено: {counts['skipped']} | {archive_cell} | "
            f"⚠️ Ошибок: {counts['errors']}"
        ),
    ]
    if not report.dry_run and not archiving_on:
        lines.append("<i>Архивация выключена — письма остаются в инбоксе.</i>")

    if report.recorded:
        lines.append("\n<b>Записано:</b>")
        lines.extend(f"  {line}" for line in report.recorded)

    if report.dry_run:
        # Preview only — nothing is in the ledger yet, so no ids.
        if report.pending:
            lines.append(f"\n❓ <b>Требуют категории ({len(report.pending)}):</b>")
            lines.extend(f"  {line}" for line in report.pending)
    elif open_pending_rows:
        # Ask about EVERY open pending (this run's + carried over), with ids,
        # phrased as a question the user can answer directly in this topic.
        lines.append(f"\n❓ <b>Куда отнести эти траты? ({len(open_pending_rows)})</b>")
        for row in open_pending_rows:
            amount = row["amount"]
            amount_str = f"{amount:,.0f}" if amount is not None else "?"
            lines.append(
                f"  #{row['id']} [{row['source']}] {amount_str} {row['currency'] or ''} — {row['description']}"
            )
        lines.append(
            "\nОтветь прямо здесь: «#id категория» через запятую (напр. «#12 Travel, #13 Food»), или «пропусти #id»."
        )

    return "\n".join(lines)
