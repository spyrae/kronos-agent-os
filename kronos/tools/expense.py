"""Direct expense tool — writes to Notion with automatic FIFO budget tracking.

Bypasses MCP/task_agent entirely. The supervisor calls this tool
directly with structured parameters. All logic is deterministic Python:
- Reads BUDGET.md for current FIFO tranches
- Calculates RUB amount from the oldest tranche rate
- Writes to Notion
- Updates BUDGET.md (deducts from tranche)
"""

import json
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from kronos.config import settings

log = logging.getLogger("kronos.tools.expense")

NOTION_VERSION = "2022-06-28"

# User timezone — Bali/KL (UTC+8)
USER_TZ = timezone(timedelta(hours=8))

# Max days in the past/future for date sanity check
DATE_MAX_PAST_DAYS = 30
DATE_MAX_FUTURE_DAYS = 1

VALID_CATEGORIES = {
    "Food",
    "Transport",
    "Subscriptions",
    "Shopping",
    "Travel",
    "Health",
    "Entertainment",
    "Other",
}

# Canonical expense schema:
# - BUDGET.md tranche carries two rates: `rate` = IDR per 1 RUB, `rate_usd` = IDR per 1 USD.
# - IDR expenses: Notion `Rate` stores the IDR/RUB value (for example 233.5) and
#   `Rate_USD` the IDR/USD value (for example 16300).
# - IDR expenses: `Amount_RUB` = round(Amount_IDR / Rate), `Amount_USD` = round(Amount_IDR / Rate_USD).
# - RUB expenses: `Amount_RUB` stores the original amount, no Rate/Amount_IDR/Amount_USD/FIFO.
# - USD expenses: `Amount_USD` stores the original amount, no Rate_USD/Amount_IDR/FIFO.
# Do not invert the rates (they are IDR per 1 unit, never unit per IDR or per 1000 IDR).
# Legacy tranches without an IDR/USD rate still yield Amount_RUB; Amount_USD is left empty
# until the tranche carries a USD rate.

# Result of a FIFO pass over IDR tranches. amount_usd / rate_usd are None when any
# consumed tranche lacks an IDR/USD rate (legacy budget) — RUB is always computed.
FifoResult = namedtuple("FifoResult", ["amount_rub", "rate_rub", "amount_usd", "rate_usd", "tranches"])


def _today() -> str:
    """Today's date in user timezone (UTC+8)."""
    return datetime.now(USER_TZ).strftime("%Y-%m-%d")


def _validate_date(date_str: str | None) -> tuple[str, str | None]:
    """Validate and sanitize date. Returns (date, warning_or_none).

    - None/empty → today (user TZ)
    - Unparseable → today + warning
    - Too far in past/future → today + warning
    """
    today = _today()
    if not date_str:
        return today, None

    # Try parsing
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        warning = f"[DATE FIX] Невалидная дата '{date_str}', заменена на {today}"
        log.warning(warning)
        return today, warning

    # Sanity check — not too far from today
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    delta = (today_dt - parsed).days

    if delta > DATE_MAX_PAST_DAYS:
        warning = f"[DATE FIX] Дата '{date_str}' слишком давняя ({delta} дней назад), заменена на {today}"
        log.warning(warning)
        return today, warning

    if delta < -DATE_MAX_FUTURE_DAYS:
        warning = f"[DATE FIX] Дата '{date_str}' в будущем ({-delta} дней вперёд), заменена на {today}"
        log.warning(warning)
        return today, warning

    return date_str, None


def _budget_path() -> str:
    from kronos.workspace import ws

    return str(ws.skill_ref("expense-tracker", "BUDGET"))


def _expenses_db_id() -> str:
    """Return the configured Notion Expenses database ID."""
    return os.environ.get("NOTION_EXPENSES_DB_ID", "").strip()


def _parse_tranches(text: str) -> list[dict]:
    """Parse active tranches from BUDGET.md table."""
    tranches = []
    in_active = False
    for line in text.splitlines():
        if "Активные транши" in line:
            in_active = True
            continue
        if in_active and line.startswith("## "):
            break
        if not in_active:
            continue
        # Parse table row. New format has an extra IDR/USD rate column:
        #   | # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Курс (IDR/USD) | Заметка |
        # Legacy format has no USD rate:
        #   | # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Заметка |
        if line.startswith("|") and not line.startswith("|---") and not line.startswith("| #"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 5:
                try:
                    num = int(cells[0].strip())
                    date = cells[1].strip()
                    total = float(cells[2].replace(",", "").replace(" ", ""))
                    remaining = float(cells[3].replace(",", "").replace(" ", ""))
                    rate = float(cells[4].replace(",", "").replace(" ", ""))
                    # Optional IDR/USD rate column. If cells[5] parses as a number it is
                    # the USD rate (new format); otherwise it is the note (legacy format).
                    rate_usd = None
                    note = ""
                    extra = cells[5:]
                    if extra:
                        try:
                            rate_usd = float(extra[0].replace(",", "").replace(" ", ""))
                            note = extra[1].strip() if len(extra) > 1 else ""
                        except ValueError:
                            note = extra[0].strip()
                    tranches.append(
                        {
                            "num": num,
                            "date": date,
                            "total": total,
                            "remaining": remaining,
                            "rate": rate,
                            "rate_usd": rate_usd,
                            "note": note,
                        }
                    )
                except (ValueError, IndexError):
                    continue
    return tranches


def _update_budget(text: str, tranches: list[dict]) -> str:
    """Rebuild the active tranches table in BUDGET.md.

    Strategy: find everything between '## Активные транши' and the next '## ' section,
    replace it entirely with the new table. No line-by-line fragility.
    """
    # Split into: before active section, active section, after active section
    marker_start = "## Активные транши"
    idx_start = text.find(marker_start)
    if idx_start == -1:
        return text

    # Find the next ## section after active tranches
    after_header = text[idx_start + len(marker_start) :]
    idx_next = after_header.find("\n## ")
    if idx_next == -1:
        before = text[:idx_start]
        after = ""
    else:
        before = text[:idx_start]
        after = after_header[idx_next + 1 :]  # +1 to skip the \n

    # Build new active section
    table_lines = [
        marker_start,
        "",
        "| # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Курс (IDR/USD) | Заметка |",
        "|---|------|-----------|-------------|-----------------|-----------------|---------|",
    ]
    for t in tranches:
        rate_usd = t.get("rate_usd")
        rate_usd_str = f"{rate_usd:g}" if rate_usd else ""
        table_lines.append(
            f"| {t['num']} | {t['date']} | {t['total']:,.0f} | {t['remaining']:,.0f} | "
            f"{t['rate']} | {rate_usd_str} | {t['note']} |"
        )
    table_lines.append("")

    return before + "\n".join(table_lines) + "\n" + after


def _write_text_atomic(path: str, text: str) -> None:
    """Write text to a file via atomic replace."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-budget-", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _fifo_calculate(amount_idr: float, tranches: list[dict]) -> FifoResult:
    """FIFO: calculate RUB + USD amounts and deduct from tranches.

    Tranche rates are IDR per 1 unit: `rate` = IDR/RUB, `rate_usd` = IDR/USD.
    Formulas: amount_rub = amount_idr / rate, amount_usd = amount_idr / rate_usd.

    USD is only reported when every consumed tranche carries a USD rate — a legacy
    tranche without `rate_usd` leaves amount_usd/rate_usd as None (RUB still computed).

    Returns: FifoResult(amount_rub, rate_rub, amount_usd, rate_usd, updated_tranches)
    """
    remaining = amount_idr
    total_rub = 0.0
    total_usd = 0.0
    usd_complete = True

    for t in tranches:
        if remaining <= 0:
            break
        available = t["remaining"]
        if available <= 0:
            continue

        take = min(remaining, available)
        # rate = IDR per 1 RUB → rub = idr / rate
        total_rub += take / t["rate"]
        # rate_usd = IDR per 1 USD → usd = idr / rate_usd
        rate_usd = t.get("rate_usd")
        if rate_usd and rate_usd > 0:
            total_usd += take / rate_usd
        else:
            usd_complete = False
        t["remaining"] -= take
        remaining -= take

    if remaining > 0:
        log.warning("FIFO: not enough budget! Deficit: %s IDR", remaining)

    effective_rate = amount_idr / total_rub if total_rub > 0 else 0

    amount_usd = None
    effective_rate_usd = None
    if usd_complete and total_usd > 0:
        amount_usd = round(total_usd, 2)
        effective_rate_usd = amount_idr / total_usd

    return FifoResult(round(total_rub), effective_rate, amount_usd, effective_rate_usd, tranches)


def _notion_rate(rate_idr_per_rub: float) -> float:
    """Return Notion `Rate` value in the canonical IDR/RUB format."""
    return round(rate_idr_per_rub, 1)


def _notion_rate_usd(rate_idr_per_usd: float) -> float:
    """Return Notion `Rate_USD` value in the canonical IDR/USD format."""
    return round(rate_idr_per_usd, 1)


def _notion_headers() -> dict[str, str]:
    token = settings.notion_api_key
    if not token:
        raise RuntimeError("NOTION_API_KEY not configured")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _notion_request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers=_notion_headers(),
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    return json.loads(resp.read())


def _notion_create_page(properties: dict) -> dict:
    """Create a page in Notion Expenses DB. Returns the created page or raises."""
    database_id = _expenses_db_id()
    if not database_id:
        raise RuntimeError("NOTION_EXPENSES_DB_ID not configured")

    return _notion_request_json(
        "POST",
        "https://api.notion.com/v1/pages",
        {
            "parent": {"database_id": database_id},
            "properties": properties,
        },
    )


def _normalize_expense_description(value: str) -> str:
    """Normalize descriptions for duplicate detection."""
    return " ".join(value.casefold().split())


def _page_title(page: dict) -> str:
    title = page.get("properties", {}).get("Description", {}).get("title", [])
    return "".join(item.get("plain_text", "") for item in title)


def _query_duplicate_candidates(date: str, amount_idr: float) -> list[dict]:
    database_id = _expenses_db_id()
    if not database_id:
        return []
    data = _notion_request_json(
        "POST",
        f"https://api.notion.com/v1/databases/{database_id}/query",
        {
            "filter": {
                "and": [
                    {"property": "Date", "date": {"equals": date}},
                    {"property": "Amount_IDR", "number": {"equals": amount_idr}},
                ]
            },
            "page_size": 20,
        },
    )
    return list(data.get("results", []))


def _archive_page(page_id: str) -> None:
    _notion_request_json(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"archived": True},
    )


def _cleanup_incomplete_duplicates(
    *,
    description: str,
    amount_idr: float,
    date: str,
    keep_page_id: str,
) -> int:
    """Archive IDR-only duplicate pages created by non-canonical writers."""
    expected_description = _normalize_expense_description(description)
    archived = 0
    for page in _query_duplicate_candidates(date, amount_idr):
        page_id = page.get("id", "")
        if not page_id or page_id == keep_page_id:
            continue
        if _normalize_expense_description(_page_title(page)) != expected_description:
            continue

        props = page.get("properties", {})
        amount_rub = props.get("Amount_RUB", {}).get("number")
        rate = props.get("Rate", {}).get("number")
        if amount_rub is not None and rate is not None:
            continue

        try:
            _archive_page(page_id)
        except Exception as e:
            log.warning("Failed to archive incomplete duplicate expense %s: %s", page_id, e)
            continue
        archived += 1
        log.info("Archived incomplete duplicate expense page: %s", page_id)
    return archived


def _schedule_duplicate_cleanup(
    *,
    description: str,
    amount_idr: float,
    date: str,
    keep_page_id: str,
) -> None:
    """Run duplicate cleanup now and once more after a short delay."""
    try:
        _cleanup_incomplete_duplicates(
            description=description,
            amount_idr=amount_idr,
            date=date,
            keep_page_id=keep_page_id,
        )
    except Exception as e:
        log.warning("Immediate duplicate cleanup failed: %s", e)

    delay = float(os.environ.get("EXPENSE_DUPLICATE_CLEANUP_DELAY_SECONDS", "5"))
    if delay <= 0:
        return

    def delayed_cleanup() -> None:
        time.sleep(delay)
        try:
            archived = _cleanup_incomplete_duplicates(
                description=description,
                amount_idr=amount_idr,
                date=date,
                keep_page_id=keep_page_id,
            )
            if archived:
                log.info("Delayed duplicate cleanup archived %d page(s)", archived)
        except Exception as e:
            log.warning("Delayed duplicate cleanup failed: %s", e)

    threading.Thread(target=delayed_cleanup, daemon=True).start()


@tool
def add_expense(
    description: str,
    amount: float,
    currency: str,
    category: str,
    date: str | None = None,
    split: bool = False,
    split_full: bool = False,
    ref: str | None = None,
) -> str:
    """Add an expense to Notion with optional automatic FIFO budget calculation.

    For IDR expenses, automatically reads BUDGET.md, converts to BOTH RUB and USD
    from FIFO tranches, writes to Notion, and updates the budget. No need to provide
    rates. For RUB expenses, writes Amount_RUB as-is; for USD expenses, writes
    Amount_USD as-is. RUB and USD expenses never touch the IDR tranches.

    IMPORTANT: Do NOT pass `date` unless the user explicitly says a different date.
    The tool uses today's date automatically. Wrong dates will be auto-corrected.

    Args:
        description: What was purchased (e.g. "Кафе", "Grab", "Продукты")
        amount: Transaction amount (e.g. 300870 IDR, 496 RUB, or 12.50 USD)
        currency: "IDR", "RUB", or "USD"
        category: One of: Food, Transport, Subscriptions, Shopping, Travel, Health, Entertainment, Other
        date: DO NOT SET unless user specified a date. Auto-defaults to today. Dates >30 days ago are rejected.
        split: True if only your RUB/USD share is halved while the original IDR
            amount stays whole (a 50/50 split with Sasha where the full charge left
            the shared IDR budget). Ticks the Notion Split checkbox.
        split_full: True if the WHOLE charge is halved before recording — the
            amount itself, the FIFO budget deduction and every converted amount.
            For shared cards (e.g. Maybank) where only half of each charge is ours.
            Also ticks the Notion Split checkbox. Do not combine with `split`.
        ref: Reference number for deduplication (optional).
    """
    # Validate category
    if category not in VALID_CATEGORIES:
        return f"[ERROR] Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"

    # Validate currency
    currency = currency.upper()
    if currency not in ("IDR", "RUB", "USD"):
        return f"[ERROR] Invalid currency '{currency}'. Must be IDR, RUB, or USD."

    # Validate and sanitize date (catches LLM hallucinating wrong year/month)
    date, date_warning = _validate_date(date)

    # split_full halves the WHOLE charge up front — the amount, and therefore the
    # FIFO budget deduction, Amount_IDR and every converted amount downstream. It
    # differs from `split`, which keeps Amount_IDR whole and only halves the
    # converted RUB/USD share. Both tick the Notion Split checkbox (mark_split).
    mark_split = split or split_full
    if split_full:
        amount = round(amount / 2, 2) if currency == "USD" else round(amount / 2)
        split = False  # amount is already halved — do not halve the share again

    # --- FIFO budget calculation for IDR ---
    amount_rub = None
    amount_usd = None
    rate_idr_per_rub = None
    rate_idr_per_usd = None
    budget_updated = False
    budget_file = ""
    budget_new_text = None
    tranches: list[dict] = []

    if currency == "IDR":
        budget_file = _budget_path()
        if os.path.isfile(budget_file):
            with open(budget_file) as f:
                budget_text = f.read()

            tranches = _parse_tranches(budget_text)
            if tranches:
                total_remaining = sum(t["remaining"] for t in tranches)
                if total_remaining >= amount:
                    fifo = _fifo_calculate(amount, tranches)
                    amount_rub = fifo.amount_rub
                    rate_idr_per_rub = fifo.rate_rub
                    amount_usd = fifo.amount_usd
                    rate_idr_per_usd = fifo.rate_usd
                    tranches = fifo.tranches
                    if split:
                        if amount_rub:
                            amount_rub = round(amount_rub / 2)
                        if amount_usd:
                            amount_usd = round(amount_usd / 2, 2)
                    budget_new_text = _update_budget(budget_text, tranches)
                else:
                    log.warning("FIFO: insufficient budget (%s IDR available, need %s)", total_remaining, amount)
            else:
                log.warning("No active tranches in BUDGET.md")
        else:
            log.warning("BUDGET.md not found at %s", budget_file)
    elif currency == "RUB":
        amount_rub = round(amount)
        if split:
            amount_rub = round(amount_rub / 2)
    elif currency == "USD":
        amount_usd = round(amount, 2)
        if split:
            amount_usd = round(amount_usd / 2, 2)

    # --- Build Notion properties ---
    properties: dict = {
        "Description": {"title": [{"text": {"content": description}}]},
        "Date": {"date": {"start": date}},
        "Category": {"select": {"name": category}},
        "Split": {"checkbox": mark_split},
        "Status": {"select": {"name": "Processed"}},
    }

    if currency == "IDR":
        properties["Amount_IDR"] = {"number": amount}

    if amount_rub is not None:
        properties["Amount_RUB"] = {"number": amount_rub}
    if amount_usd is not None:
        properties["Amount_USD"] = {"number": amount_usd}
    if rate_idr_per_rub is not None:
        properties["Rate"] = {"number": _notion_rate(rate_idr_per_rub)}
    if rate_idr_per_usd is not None:
        properties["Rate_USD"] = {"number": _notion_rate_usd(rate_idr_per_usd)}

    if ref:
        properties["Ref"] = {"rich_text": [{"text": {"content": ref}}]}

    # --- Write to Notion ---
    try:
        result = _notion_create_page(properties)
    except Exception as e:
        log.error("Failed to create expense: %s", e)
        return f"[ERROR] Failed to write to Notion: {e}"

    page_id = result.get("id", "unknown")
    log.info("Expense created: %s %s %s → page %s", description, amount, currency, page_id)

    if currency == "IDR" and page_id != "unknown":
        _schedule_duplicate_cleanup(
            description=description,
            amount_idr=amount,
            date=date,
            keep_page_id=page_id,
        )

    budget_warning = None
    if budget_new_text is not None and budget_file:
        try:
            _write_text_atomic(budget_file, budget_new_text)
        except Exception as e:
            log.error("Expense created, but failed to update budget: %s", e)
            budget_warning = f"[BUDGET ERROR] Notion записан, но бюджет не обновлён: {e}"
        else:
            budget_updated = True
            log.info(
                "FIFO: %s IDR → %s RUB (rate %.1f) / %s USD (rate %s), budget updated",
                amount,
                amount_rub,
                rate_idr_per_rub or 0,
                amount_usd if amount_usd is not None else "—",
                f"{rate_idr_per_usd:.1f}" if rate_idr_per_usd else "—",
            )

    if currency == "RUB":
        amount_display = f"{amount:,.0f} ₽"
    elif currency == "USD":
        amount_display = f"{amount:,.2f} $"
    else:
        amount_display = f"{amount:,.0f} {currency}"
    parts = [f"✅ '{description}' — {amount_display}"]
    if currency == "IDR":
        conv = []
        if amount_rub is not None:
            conv.append(f"{amount_rub:,} ₽")
        if amount_usd is not None:
            conv.append(f"{amount_usd:,.2f} $")
        if conv:
            parts.append("= " + " / ".join(conv))
    if mark_split:
        parts.append("(split, твоя доля)")
    parts.append(f"| Дата: {date}")
    if budget_updated:
        remaining = sum(t["remaining"] for t in tranches)
        parts.append(f"| Остаток: {remaining:,.0f} IDR")
    if budget_warning:
        parts.append(f"| ⚠️ {budget_warning}")
    if date_warning:
        parts.append(f"| ⚠️ {date_warning}")
    return " ".join(parts)


@tool
def add_tranche(
    amount_idr: float,
    rate: float,
    rate_usd: float | None = None,
    note: str = "",
) -> str:
    """Add a new IDR budget tranche for FIFO expense tracking.

    Call when the user says something like "у нас 10 млн рупий, курс рубля 207.6,
    курс доллара 16300" or "пополни бюджет 7 млн по 195 и 16250".

    Args:
        amount_idr: Amount in IDR (e.g. 10000000 for 10 million)
        rate: Exchange rate as IDR per 1 RUB (e.g. 207.6 means 207.6 IDR = 1 RUB)
        rate_usd: Exchange rate as IDR per 1 USD (e.g. 16300 means 16300 IDR = 1 USD).
            Optional, but pass it with every new tranche so IDR expenses convert to USD too.
        note: Optional note (e.g. "Второй транш")
    """
    budget_file = _budget_path()
    if not os.path.isfile(budget_file):
        return f"[ERROR] BUDGET.md not found at {budget_file}"

    with open(budget_file) as f:
        text = f.read()

    tranches = _parse_tranches(text)
    next_num = max((t["num"] for t in tranches), default=0) + 1
    today = datetime.now(USER_TZ).strftime("%d.%m.%Y")

    tranches.append(
        {
            "num": next_num,
            "date": today,
            "total": amount_idr,
            "remaining": amount_idr,
            "rate": rate,
            "rate_usd": rate_usd,
            "note": note or f"Транш #{next_num}",
        }
    )

    new_text = _update_budget(text, tranches)
    with open(budget_file, "w") as f:
        f.write(new_text)

    total_remaining = sum(t["remaining"] for t in tranches)
    log.info("Tranche added: %s IDR at %s IDR/RUB, %s IDR/USD", amount_idr, rate, rate_usd or "—")
    total_rub = sum(t["remaining"] / t["rate"] for t in tranches if t["rate"] > 0)
    total_usd = sum(t["remaining"] / t["rate_usd"] for t in tranches if t.get("rate_usd"))
    rate_desc = f"{rate} IDR/RUB" + (f", {rate_usd:g} IDR/USD" if rate_usd else "")
    usd_note = f" / ≈ ${total_usd:,.0f}" if total_usd > 0 else ""
    return (
        f"OK: Транш #{next_num} добавлен — {amount_idr:,.0f} IDR по курсу {rate_desc}. "
        f"Общий остаток: {total_remaining:,.0f} IDR ≈ {total_rub:,.0f} ₽{usd_note} ({len(tranches)} траншей)"
    )


@tool
def replace_tranche(
    tranche_num: int,
    new_rate: float,
    new_rate_usd: float | None = None,
    new_amount_idr: float | None = None,
    note: str = "",
) -> str:
    """Replace an existing tranche with new rates (and optionally new amount).

    Use when the user says "поменяй курс транша 1 на 207.6 и 16300" or
    "обнови транш — остаток тот же, курс 210".

    Args:
        tranche_num: Number of the tranche to replace (e.g. 1)
        new_rate: New exchange rate as IDR per 1 RUB (e.g. 207.6)
        new_rate_usd: New exchange rate as IDR per 1 USD (e.g. 16300). If not provided,
            keeps the tranche's current USD rate.
        new_amount_idr: New IDR amount. If not provided, keeps the current remaining amount.
        note: Optional note for the new tranche.
    """
    budget_file = _budget_path()
    if not os.path.isfile(budget_file):
        return "[ERROR] BUDGET.md not found"

    with open(budget_file) as f:
        text = f.read()

    tranches = _parse_tranches(text)
    target = next((t for t in tranches if t["num"] == tranche_num), None)
    if not target:
        nums = [t["num"] for t in tranches]
        return f"[ERROR] Транш #{tranche_num} не найден. Доступные: {nums}"

    old_rate = target["rate"]
    old_rate_usd = target.get("rate_usd")

    # Update in place
    if new_amount_idr is not None:
        target["total"] = new_amount_idr
        target["remaining"] = new_amount_idr
    target["rate"] = new_rate
    if new_rate_usd is not None:
        target["rate_usd"] = new_rate_usd
    if note:
        target["note"] = note

    new_text = _update_budget(text, tranches)
    with open(budget_file, "w") as f:
        f.write(new_text)

    effective_rate_usd = target.get("rate_usd")
    log.info(
        "Tranche #%d updated: rate %s → %s IDR/RUB, USD %s → %s",
        tranche_num,
        old_rate,
        new_rate,
        old_rate_usd or "—",
        effective_rate_usd or "—",
    )
    usd_part = ""
    if effective_rate_usd:
        usd_part = f", {old_rate_usd or '—'} → {effective_rate_usd:g} IDR/USD"
        usd_part += f" ≈ ${target['remaining'] / effective_rate_usd:,.0f}"
    return (
        f"OK: Транш #{tranche_num} обновлён — курс {old_rate} → {new_rate} IDR/RUB{usd_part}. "
        f"Остаток: {target['remaining']:,.0f} IDR ≈ {target['remaining'] / new_rate:,.0f} ₽"
    )


@tool
def get_budget() -> str:
    """Show current budget status — active tranches, remaining amounts, rates.

    Call when the user asks "какой остаток?", "сколько осталось?", "бюджет", "транши".
    """
    budget_file = _budget_path()
    if not os.path.isfile(budget_file):
        return "[ERROR] BUDGET.md not found"

    with open(budget_file) as f:
        text = f.read()

    tranches = _parse_tranches(text)
    if not tranches:
        return "Нет активных траншей. Попроси пользователя добавить бюджет."

    lines = ["**Активные транши:**"]
    total_remaining = 0
    for t in tranches:
        total_remaining += t["remaining"]
        rub = t["remaining"] / t["rate"] if t["rate"] > 0 else 0
        pct = (t["remaining"] / t["total"] * 100) if t["total"] > 0 else 0
        rate_usd = t.get("rate_usd")
        if rate_usd:
            usd = t["remaining"] / rate_usd
            rate_part = f"курс {t['rate']} IDR/RUB, {rate_usd:g} IDR/USD"
            money_part = f"≈ {rub:,.0f} ₽ / ${usd:,.0f}"
        else:
            rate_part = f"курс {t['rate']} IDR/RUB"
            money_part = f"≈ {rub:,.0f} ₽"
        lines.append(
            f"  #{t['num']} | {t['remaining']:,.0f} / {t['total']:,.0f} IDR "
            f"({pct:.0f}%) | {rate_part} | {money_part} | {t['note']}"
        )

    total_rub = sum(t["remaining"] / t["rate"] for t in tranches if t["rate"] > 0)
    total_usd = sum(t["remaining"] / t["rate_usd"] for t in tranches if t.get("rate_usd"))
    usd_total = f" / ${total_usd:,.0f}" if total_usd > 0 else ""
    lines.append(f"\n**Итого:** {total_remaining:,.0f} IDR ≈ {total_rub:,.0f} ₽{usd_total}")
    return "\n".join(lines)
