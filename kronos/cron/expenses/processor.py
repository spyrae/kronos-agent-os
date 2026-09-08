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
           low category confidence     → category := Other (still recorded)
           audit fails (LLM, 2nd pass) → pending
           else                        → add_expense (FIFO IDR→RUB→USD)
      └─ finish only when EVERY item is terminal; otherwise retain the email
         retry definite failures from the frozen per-item snapshot
         uncertain writes require reconciliation, never blind replay

Ordering: Maybank is searched FIRST because it is the only split source and a
Grab ride paid by the Maybank card arrives from BOTH Grab and Maybank — the
Maybank copy must land first so the charge is halved (split), and the Grab copy
dedups against it. A Grab ride NOT paid by Maybank has no Maybank copy, so it is
recorded whole (not split) — exactly "split only when it overlaps Maybank".
Grab still precedes wondr/permata so its richer record wins over those banks
(the user's "dedup by amount+date, keep the more detailed one" choice).

An unrecognised category never blocks a charge: a missing category, or one the
extractor is not confident about, is written as ``FALLBACK_CATEGORY`` instead of
being parked for the user to classify from chat. Only what genuinely cannot be
written — an unsupported currency, or an expense the audit pass rejected — still
lands in the pending queue.

Every run posts a report to the finance topic — always, even on empty runs —
listing how many emails were scanned, what was recorded (with amounts), what was
deduped/skipped, and what could not be written. ``dry_run=True`` performs
extraction + audit but writes nothing and archives nothing: a safe way to see
what a real run would do against the live mailbox.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime

from kronos.config import settings
from kronos.cron.expenses.extract import (
    SUPPORTED_CURRENCIES,
    ExpenseExtractionError,
    ExtractedExpense,
    audit_expense,
    extract_expenses,
)
from kronos.cron.expenses.gmail import archiving_enabled, get_gmail_client
from kronos.cron.expenses.ledger import get_ledger
from kronos.cron.notify import TOPIC_FINANCE, send_bot_api
from kronos.tools.expense import FALLBACK_RATE_NOTE, USER_TZ

log = logging.getLogger("kronos.cron.expenses.processor")

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
# Category used when the extractor returns none, or is not confident enough. The
# charge is recorded under it rather than queued as a question in the chat: a
# slightly-off category is cheaper to fix later than a missing expense.
FALLBACK_CATEGORY = "Other"
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_SEARCH_LIMIT = 25

# Sources whose charges are shared 50/50 and must be halved in full before they
# reach Notion (amount, FIFO budget and every converted amount), then flagged
# with the Split checkbox. See add_expense(split_full=...).
SPLIT_SOURCES = frozenset({"maybank"})


def _is_split_source(source: str | None) -> bool:
    return (source or "").lower() in SPLIT_SOURCES


def _today() -> str:
    return datetime.now(USER_TZ).strftime("%Y-%m-%d")


def _threshold() -> float:
    try:
        return float(os.environ.get("EXPENSES_CATEGORY_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD))
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD


def _source_queries() -> list[tuple[str, str]]:
    """(source, Gmail query) pairs. Maybank first (split priority), then Grab.

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
    # ``-from:e-statement`` drops the monthly Consolidated Statement (a PDF summary
    # of the whole month, not a single charge) that also comes from @maybank.co.id.
    maybank = os.environ.get("EMAIL_EXPENSES_QUERY_MAYBANK", f"{win} from:maybank -from:e-statement")
    # Maybank FIRST so a card-paid Grab charge is halved before its Grab copy is
    # seen (see module docstring). Grab still precedes wondr/permata for detail.
    return [("maybank", maybank), ("grab", grab), ("wondr", wondr), ("permata", permata)]


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
        self.pending: list[str] = []  # lines for charges that could not be written
        self.sources: list[str] = []  # sources that returned mail
        self.stale_rate: list[str] = []  # charges priced past the end of the budget
        self.errors: list[str] = []

    def add_recorded(self, line: str) -> None:
        self.recorded.append(line)

    def add_stale_rate(self, line: str) -> None:
        self.stale_rate.append(line)

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

    # Retry known failures independently of Gmail's rolling lookback window.
    for row in ledger.list_retryable(limit=DEFAULT_SEARCH_LIMIT):
        source = row["source"] or "other"
        source_by_id.setdefault(row["message_id"], source)
        if source not in report.sources:
            report.sources.append(source)

    # 2) Drop anything already handled or already queued as pending.
    todo = [mid for mid in source_by_id if ledger.needs_processing(mid)]

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
    uncertain = ledger.uncertain_items()
    counts["errors"] += len(uncertain)
    for row in uncertain:
        label = f"Позиция {row['item_index'] + 1}" if row["item_index"] >= 0 else "Запись"
        report.errors.append(
            f"[{row['source']}] {label} письма {row['message_id']}: "
            "результат записи неизвестен; нужна сверка, автоматический повтор остановлен."
        )
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
    try:
        items = ledger.list_items(msg.message_id)
        if items:
            msg.source = items[0]["source"]
        expenses = (
            [ExtractedExpense(**json.loads(row["expense_json"])) for row in items]
            if items
            else extractor(msg, model=model)
        )
        if not isinstance(expenses, list):
            raise ExpenseExtractionError("extractor did not return a list")
    except Exception as exc:
        # A timeout/malformed answer is not evidence that this is non-expense
        # mail. Keep it non-terminal and continue processing other messages.
        reason = f"extraction failed: {type(exc).__name__}"
        log.warning("Email expense %s", reason)
        counts["errors"] += 1
        report.errors.append(f"[{msg.source}] Не удалось разобрать письмо; будет повторная попытка.")
        if not dry_run:
            ledger.record(message_id=msg.message_id, source=msg.source, status="error", error=reason)
        return
    if not expenses:
        # Not a spend email (top-up, transfer, marketing). Handled, not archived.
        if not dry_run:
            ledger.record(message_id=msg.message_id, source=msg.source, status="skipped")
        counts["skipped"] += 1
        return

    if not items and not dry_run:
        # Freeze the extraction (including fallback dates) before any write;
        # retries must not ask the LLM to invent a different item ordering.
        expenses = [replace(exp, expense_date=exp.expense_date or _today()) for exp in expenses]
        items = ledger.prepare_items(msg.message_id, msg.source, expenses)
        msg.source = items[0]["source"]
        expenses = [ExtractedExpense(**json.loads(row["expense_json"])) for row in items]

    for index, exp in enumerate(expenses):
        if items and items[index]["status"] not in {"ready", "error"}:
            continue
        if not dry_run and not ledger.claim_item(msg.message_id, index):
            continue
        try:
            outcome, _, _ = _handle_expense(
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
                item_index=index,
                ref=msg.message_id if len(expenses) == 1 else f"{msg.message_id}:{index + 1}",
            )
        except Exception as exc:
            # An unexpected failure might happen after the write (e.g. while
            # rendering its result). Never convert that into a blind retry.
            log.warning("Expense outcome unknown (%s)", type(exc).__name__)
            outcome = "uncertain"
            if dry_run:
                counts["errors"] += 1
                report.errors.append(f"[{msg.source}] Проверка позиции {index + 1} не удалась; запись не выполнялась.")
        if not dry_run:
            ledger.finish_item(msg.message_id, index, outcome)

    if not dry_run:
        status = ledger.finalize_message(msg.message_id, msg.source)
        if status in {"recorded", "duplicate"}:
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
    item_index: int,
    ref: str,
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
            item_index=item_index,
            reason=f"unsupported currency {exp.currency}",
            counts=counts,
            report=report,
            dry_run=dry_run,
        )
        return "pending", None, None

    dup_key = (amount_idr, date, msg.message_id)
    is_dup = ledger.find_recorded_duplicate(amount_idr, date, exclude_message_id=msg.message_id) is not None
    if dry_run and amount_idr is not None:
        is_dup = is_dup or any(key[:2] == dup_key[:2] and key[2] != msg.message_id for key in seen_dry)
    if is_dup:
        counts["duplicates"] += 1
        return "duplicate", None, None

    # No category, or one the extractor is unsure of, is not a reason to stop:
    # record it as FALLBACK_CATEGORY. The audit pass below still runs and its
    # own category suggestion wins over the fallback when it has one.
    used_fallback = exp.category is None or exp.confidence < threshold
    if used_fallback:
        exp = replace(exp, category=FALLBACK_CATEGORY)

    try:
        verdict = auditor(msg.text, exp, model=model)
    except Exception as exc:
        log.warning("Expense audit failed (%s)", type(exc).__name__)
        counts["errors"] += 1
        report.errors.append(f"[{msg.source}] Проверка позиции {item_index + 1} не удалась; будет повтор.")
        return "error", None, None
    if not (verdict.ok and verdict.amount_matches and verdict.is_expense):
        _queue_pending(
            ledger,
            msg,
            exp,
            amount_idr,
            date,
            item_index=item_index,
            reason=f"audit rejected: {verdict.issues or 'unverified'}",
            counts=counts,
            report=report,
            dry_run=dry_run,
        )
        return "pending", None, None

    category = verdict.category or exp.category
    # Flag only a charge that actually kept the fallback — an audit-supplied
    # category means it was classified after all.
    fallback_note = " ⟨категория по умолчанию⟩" if used_fallback and category == FALLBACK_CATEGORY else ""

    if dry_run:
        if amount_idr is not None:
            seen_dry.add(dup_key)
        split_note = " ÷2 split" if _is_split_source(msg.source) else ""
        report.add_recorded(
            f"🔎 [{msg.source}] {exp.amount:,.0f} {exp.currency}{split_note} — {exp.description} "
            f"→ {category} (conf {exp.confidence:.0%}, audit ✓){fallback_note}"
        )
        counts["recorded"] += 1
        return "recorded", amount_idr, date

    try:
        result = expense_writer(
            {
                "description": exp.description,
                "amount": exp.amount,
                "currency": exp.currency,
                "category": category,
                "date": exp.expense_date,
                "split_full": _is_split_source(msg.source),
                "ref": ref,
            }
        )
    except Exception as exc:
        log.warning("Expense write outcome unknown (%s)", type(exc).__name__)
        return "uncertain", None, None
    # The canonical writer may have sent a POST before losing its response.
    # No automatic replay can safely decide whether that POST took effect.
    if not isinstance(result, str) or result.startswith("[ERROR] Failed to write to Notion:"):
        return "uncertain", None, None
    if result.startswith("[ERROR]"):
        counts["errors"] += 1
        report.errors.append(f"[{msg.source}] Позиция {item_index + 1} не записана; будет повтор.")
        return "error", None, None
    if not result.startswith("✅"):
        return "uncertain", None, None

    report.add_recorded(f"[{msg.source}] {result}{fallback_note}")
    # add_expense converts past an exhausted budget rather than dropping the conversion;
    # its marker is the only signal, so lift it into its own block in the report.
    if FALLBACK_RATE_NOTE in result:
        report.add_stale_rate(f"[{msg.source}] {exp.amount:,.0f} {exp.currency} — {exp.description}")
    counts["recorded"] += 1
    return "recorded", amount_idr, date


def _queue_pending(ledger, msg, exp, amount_idr, date, *, item_index, reason, counts, report, dry_run) -> None:
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
            item_index=item_index,
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

    if report.stale_rate:
        lines.append(
            f"\n🏦 <b>Бюджет IDR исчерпан</b> — {len(report.stale_rate)} расход(ов) пересчитаны "
            f"по курсу последнего транша:"
        )
        lines.extend(f"  {line}" for line in report.stale_rate)
        lines.append("<i>Пополни бюджет, чтобы курс снова считался по факту покупки рупий.</i>")

    if report.recorded:
        lines.append("\n<b>Записано:</b>")
        lines.extend(f"  {line}" for line in report.recorded)

    if report.errors:
        lines.append("\n<b>Ошибки обработки:</b>")
        lines.extend(f"  {line}" for line in report.errors)

    if report.dry_run:
        # Preview only — nothing is in the ledger yet, so no ids.
        if report.pending:
            lines.append(f"\n⚠️ <b>Не удалось записать ({len(report.pending)}):</b>")
            lines.extend(f"  {line}" for line in report.pending)
    elif open_pending_rows:
        # List EVERY open pending (this run's + carried over), with ids, so the
        # user can clear it from this topic. These are charges the pipeline could
        # not write at all — an unclear category is no longer among the reasons.
        lines.append(f"\n⚠️ <b>Не удалось записать ({len(open_pending_rows)})</b>")
        for row in open_pending_rows:
            amount = row["amount"]
            amount_str = f"{amount:,.0f}" if amount is not None else "?"
            reason = f" — {row['reason']}" if row["reason"] else ""
            lines.append(
                f"  #{row['id']} [{row['source']}] {amount_str} {row['currency'] or ''} — {row['description']}{reason}"
            )
        lines.append(
            "\nОтветь прямо здесь: «#id категория» через запятую (напр. «#12 Travel, #13 Food»), или «пропусти #id»."
        )

    return "\n".join(lines)
