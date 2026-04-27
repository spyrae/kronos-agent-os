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
import urllib.request
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from kronos.config import settings

log = logging.getLogger("kronos.tools.expense")

EXPENSES_DB_ID = os.environ.get("NOTION_EXPENSES_DB_ID", "")
NOTION_VERSION = "2022-06-28"

# User timezone — Bali/KL (UTC+8)
USER_TZ = timezone(timedelta(hours=8))

# Max days in the past/future for date sanity check
DATE_MAX_PAST_DAYS = 30
DATE_MAX_FUTURE_DAYS = 1

VALID_CATEGORIES = {
    "Food", "Transport", "Subscriptions", "Shopping",
    "Travel", "Health", "Entertainment", "Other",
}


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
        # Parse table row: | # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Заметка |
        if line.startswith("|") and not line.startswith("|---") and not line.startswith("| #"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 5:
                try:
                    num = int(cells[0].strip())
                    date = cells[1].strip()
                    total = float(cells[2].replace(",", "").replace(" ", ""))
                    remaining = float(cells[3].replace(",", "").replace(" ", ""))
                    rate = float(cells[4].replace(",", "").replace(" ", ""))
                    note = cells[5].strip() if len(cells) > 5 else ""
                    tranches.append({
                        "num": num, "date": date, "total": total,
                        "remaining": remaining, "rate": rate, "note": note,
                    })
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
    after_header = text[idx_start + len(marker_start):]
    idx_next = after_header.find("\n## ")
    if idx_next == -1:
        before = text[:idx_start]
        after = ""
    else:
        before = text[:idx_start]
        after = after_header[idx_next + 1:]  # +1 to skip the \n

    # Build new active section
    table_lines = [
        marker_start,
        "",
        "| # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Заметка |",
        "|---|------|-----------|-------------|-----------------|---------|",
    ]
    for t in tranches:
        table_lines.append(
            f"| {t['num']} | {t['date']} | {t['total']:,.0f} | "
            f"{t['remaining']:,.0f} | {t['rate']} | {t['note']} |"
        )
    table_lines.append("")

    return before + "\n".join(table_lines) + "\n" + after


def _fifo_calculate(amount_idr: float, tranches: list[dict]) -> tuple[float, float, list[dict]]:
    """FIFO: calculate RUB amount and deduct from tranches.

    Rate in tranches = IDR per 1 RUB. Formula: amount_rub = amount_idr / rate.

    Returns: (amount_rub, effective_rate, updated_tranches)
    """
    remaining = amount_idr
    total_rub = 0.0

    for t in tranches:
        if remaining <= 0:
            break
        available = t["remaining"]
        if available <= 0:
            continue

        take = min(remaining, available)
        # rate = IDR per 1 RUB → rub = idr / rate
        rub_for_this = take / t["rate"]
        total_rub += rub_for_this
        t["remaining"] -= take
        remaining -= take

    if remaining > 0:
        log.warning("FIFO: not enough budget! Deficit: %s IDR", remaining)

    effective_rate = amount_idr / total_rub if total_rub > 0 else 0
    return round(total_rub), effective_rate, tranches


def _notion_create_page(properties: dict) -> dict:
    """Create a page in Notion Expenses DB. Returns the created page or raises."""
    token = settings.notion_api_key
    if not token:
        raise RuntimeError("NOTION_API_KEY not configured")

    payload = json.dumps({
        "parent": {"database_id": EXPENSES_DB_ID},
        "properties": properties,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
    )
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


@tool
def add_expense(
    description: str,
    amount: float,
    currency: str,
    category: str,
    date: str | None = None,
    split: bool = False,
    ref: str | None = None,
) -> str:
    """Add an expense to Notion with automatic FIFO budget calculation.

    Automatically reads BUDGET.md, calculates RUB from FIFO tranches,
    writes to Notion, and updates the budget. No need to provide rate.

    IMPORTANT: Do NOT pass `date` unless the user explicitly says a different date.
    The tool uses today's date automatically. Wrong dates will be auto-corrected.

    Args:
        description: What was purchased (e.g. "Кафе", "Grab", "Продукты")
        amount: Transaction amount in IDR (e.g. 300870)
        currency: Must be "IDR"
        category: One of: Food, Transport, Subscriptions, Shopping, Travel, Health, Entertainment, Other
        date: DO NOT SET unless user specified a date. Auto-defaults to today. Dates >30 days ago are rejected.
        split: True if expense is split 50/50 (with Sasha). Default False.
        ref: Reference number for deduplication (optional).
    """
    # Validate category
    if category not in VALID_CATEGORIES:
        return f"[ERROR] Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"

    # Validate currency
    currency = currency.upper()
    if currency not in ("IDR",):
        return f"[ERROR] Invalid currency '{currency}'. Must be IDR."

    # Validate and sanitize date (catches LLM hallucinating wrong year/month)
    date, date_warning = _validate_date(date)

    # --- FIFO budget calculation for IDR ---
    amount_rub = None
    rate_idr_per_rub = None
    budget_updated = False

    if currency == "IDR":
        budget_file = _budget_path()
        if os.path.isfile(budget_file):
            with open(budget_file) as f:
                budget_text = f.read()

            tranches = _parse_tranches(budget_text)
            if tranches:
                total_remaining = sum(t["remaining"] for t in tranches)
                if total_remaining >= amount:
                    amount_rub, rate_idr_per_rub, tranches = _fifo_calculate(amount, tranches)
                    if split and amount_rub:
                        amount_rub = round(amount_rub / 2)
                    # Update BUDGET.md
                    new_text = _update_budget(budget_text, tranches)
                    with open(budget_file, "w") as f:
                        f.write(new_text)
                    budget_updated = True
                    log.info("FIFO: %s IDR → %s RUB (rate %.1f), budget updated", amount, amount_rub, rate_idr_per_rub or 0)
                else:
                    log.warning("FIFO: insufficient budget (%s IDR available, need %s)", total_remaining, amount)
            else:
                log.warning("No active tranches in BUDGET.md")
        else:
            log.warning("BUDGET.md not found at %s", budget_file)

    # --- Build Notion properties ---
    properties: dict = {
        "Description": {"title": [{"text": {"content": description}}]},
        "Date": {"date": {"start": date}},
        "Category": {"select": {"name": category}},
        "Split": {"checkbox": split},
        "Status": {"select": {"name": "Processed"}},
    }

    if currency == "IDR":
        properties["Amount_IDR"] = {"number": amount}

    if amount_rub is not None:
        properties["Amount_RUB"] = {"number": amount_rub}
    if rate_idr_per_rub is not None:
        properties["Rate"] = {"number": round(rate_idr_per_rub, 1)}

    if ref:
        properties["Ref"] = {"rich_text": [{"text": {"content": ref}}]}

    # --- Write to Notion ---
    try:
        result = _notion_create_page(properties)
        page_id = result.get("id", "unknown")
        log.info("Expense created: %s %s %s → page %s", description, amount, currency, page_id)

        parts = [f"✅ '{description}' — {amount:,.0f} {currency}"]
        if amount_rub is not None:
            parts.append(f"= {amount_rub:,} ₽")
            if split:
                parts.append("(split, твоя доля)")
        parts.append(f"| Дата: {date}")
        if budget_updated:
            remaining = sum(t["remaining"] for t in tranches)
            parts.append(f"| Остаток: {remaining:,.0f} IDR")
        if date_warning:
            parts.append(f"| ⚠️ {date_warning}")
        return " ".join(parts)

    except Exception as e:
        log.error("Failed to create expense: %s", e)
        return f"[ERROR] Failed to write to Notion: {e}"


@tool
def add_tranche(
    amount_idr: float,
    rate: float,
    note: str = "",
) -> str:
    """Add a new IDR budget tranche for FIFO expense tracking.

    Call when the user says something like "у нас 10 млн рупий по курсу 207.6"
    or "пополни бюджет 7 млн по 195".

    Args:
        amount_idr: Amount in IDR (e.g. 10000000 for 10 million)
        rate: Exchange rate as IDR per 1 RUB (e.g. 207.6 means 207.6 IDR = 1 RUB)
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

    tranches.append({
        "num": next_num,
        "date": today,
        "total": amount_idr,
        "remaining": amount_idr,
        "rate": rate,
        "note": note or f"Транш #{next_num}",
    })

    new_text = _update_budget(text, tranches)
    with open(budget_file, "w") as f:
        f.write(new_text)

    total_remaining = sum(t["remaining"] for t in tranches)
    log.info("Tranche added: %s IDR at rate %s IDR/RUB", amount_idr, rate)
    total_rub = sum(t["remaining"] / t["rate"] for t in tranches if t["rate"] > 0)
    return (
        f"OK: Транш #{next_num} добавлен — {amount_idr:,.0f} IDR по курсу {rate} IDR/RUB. "
        f"Общий остаток: {total_remaining:,.0f} IDR ≈ {total_rub:,.0f} ₽ ({len(tranches)} траншей)"
    )


@tool
def replace_tranche(
    tranche_num: int,
    new_rate: float,
    new_amount_idr: float | None = None,
    note: str = "",
) -> str:
    """Replace an existing tranche with a new rate (and optionally new amount).

    Use when the user says "поменяй курс транша 1 на 4.82" or
    "обнови транш — остаток тот же, курс 5.10".

    Args:
        tranche_num: Number of the tranche to replace (e.g. 1)
        new_rate: New exchange rate as IDR per 1 RUB (e.g. 207.6)
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
    old_remaining = target["remaining"]

    # Update in place
    if new_amount_idr is not None:
        target["total"] = new_amount_idr
        target["remaining"] = new_amount_idr
    target["rate"] = new_rate
    if note:
        target["note"] = note

    new_text = _update_budget(text, tranches)
    with open(budget_file, "w") as f:
        f.write(new_text)

    log.info("Tranche #%d updated: rate %s → %s IDR/RUB", tranche_num, old_rate, new_rate)
    return (
        f"OK: Транш #{tranche_num} обновлён — курс {old_rate} → {new_rate} IDR/RUB. "
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
        lines.append(
            f"  #{t['num']} | {t['remaining']:,.0f} / {t['total']:,.0f} IDR "
            f"({pct:.0f}%) | курс {t['rate']} IDR/RUB | ≈ {rub:,.0f} ₽ | {t['note']}"
        )

    total_rub = sum(t["remaining"] / t["rate"] for t in tranches if t["rate"] > 0)
    lines.append(f"\n**Итого:** {total_remaining:,.0f} IDR ≈ {total_rub:,.0f} ₽")
    return "\n".join(lines)
