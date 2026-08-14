"""Comparing offers: arithmetic and completeness, not judgement.

"Which flat is the better deal" and "where is this console cheaper" both come
down to adding money up correctly and noticing what is missing. Models are bad
at exactly those two things and good at the part that follows — weighing a
shorter commute against a higher rent. So this module does the first part and
refuses to do the second.

What it therefore will not do: score, weight, or pick a winner on anything but
money. A function that decided a 4.6-star seller beats a 4.9-star one because
it saves 3% would be making the owner's tradeoff for them, silently and in
code.

Three refusals carry most of the value:

* **A missing field is not zero.** A listing that never states the deposit is
  not a listing with no deposit, and treating it as one makes the incomplete
  offer look cheapest. Those offers are set aside, named, and never ranked.
* **Two currencies do not add up.** Without a rate — which this module has no
  business inventing — a total mixing Rp and USD is a wrong number that looks
  right.
* **A one-off cost is not a monthly one.** A deposit and a rent are both money
  and are not the same money, so which is which has to be stated.
"""

import json
import logging
from dataclasses import dataclass, field

from langchain_core.tools import tool

from kronos.plan_conditions import format_number, parse_number

log = logging.getLogger("kronos.tools.compare")

MAX_OFFERS = 60
NAME_KEYS = ("name", "title", "label", "offer", "listing")
CURRENCY_KEYS = ("currency", "cur", "ccy")


@dataclass
class Comparison:
    ranked: list[dict] = field(default_factory=list)
    incomplete: list[dict] = field(default_factory=list)
    currency: str = ""
    periods: int = 1
    one_off: list[str] = field(default_factory=list)
    recurring: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class CompareError(Exception):
    """Raised when the offers cannot be compared at all, with the reason."""


def _split(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").replace(";", ",").split(",") if part.strip()]


def _name_of(offer: dict, index: int) -> str:
    for key in NAME_KEYS:
        value = offer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"offer {index + 1}"


def _currency_of(offer: dict) -> str:
    for key in CURRENCY_KEYS:
        value = offer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _amount(offer: dict, key: str) -> tuple[float | None, str]:
    """(value, why it is missing). A key that is absent and one that is unreadable
    are both missing, but the owner should be told which."""
    if key not in offer:
        return None, f"{key} not stated"
    raw = offer[key]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, f"{key} empty"
    if isinstance(raw, int | float):
        return float(raw), ""
    number = parse_number(str(raw))
    if number is None:
        return None, f"{key} unreadable ({str(raw)[:40]!r})"
    return number, ""


def compare(
    offers: list[dict],
    *,
    one_off: list[str] | None = None,
    recurring: list[str] | None = None,
    periods: int = 1,
) -> Comparison:
    """Total each offer and rank the complete ones. Never invents a number.

    ``recurring`` fields are multiplied by ``periods``; ``one_off`` fields are
    counted once. Anything not named in either is carried through untouched, for
    whoever weighs it.
    """
    if not offers:
        raise CompareError("no offers to compare")
    if len(offers) > MAX_OFFERS:
        raise CompareError(f"{len(offers)} offers is more than this compares at once (max {MAX_OFFERS})")
    if periods < 1:
        raise CompareError("periods must be at least 1")

    one_off = one_off or []
    recurring = recurring or []
    if not one_off and not recurring:
        raise CompareError("name at least one cost field, e.g. recurring='price' or one_off='shipping'")

    result = Comparison(periods=periods, one_off=list(one_off), recurring=list(recurring))
    for index, offer in enumerate(offers):
        if not isinstance(offer, dict):
            raise CompareError(f"offer {index + 1} is not an object")

    currencies = {_currency_of(offer) for offer in offers} - {""}
    if len(currencies) > 1:
        raise CompareError(
            f"offers are priced in {', '.join(sorted(currencies))} — convert them to one currency first; "
            f"adding them up as they are would produce a wrong number that looks right"
        )
    result.currency = next(iter(currencies), "")

    for index, offer in enumerate(offers):
        name = _name_of(offer, index)
        parts: dict[str, float] = {}
        missing: list[str] = []
        total = 0.0

        for key in recurring:
            value, why = _amount(offer, key)
            if value is None:
                missing.append(why)
                continue
            parts[key] = value * periods
            total += value * periods
        for key in one_off:
            value, why = _amount(offer, key)
            if value is None:
                missing.append(why)
                continue
            parts[key] = value
            total += value

        extras = {
            key: offer[key]
            for key in offer
            if key not in recurring and key not in one_off and key not in NAME_KEYS and key not in CURRENCY_KEYS
        }
        row = {"name": name, "total": round(total, 2), "parts": parts, "other": extras}
        if missing:
            # Set aside rather than ranked: a total that silently skips the
            # deposit is what makes the least documented offer look best.
            result.incomplete.append({**row, "missing": missing})
        else:
            result.ranked.append(row)

    result.ranked.sort(key=lambda row: row["total"])
    if result.ranked:
        cheapest = result.ranked[0]["total"]
        for row in result.ranked:
            row["over_cheapest"] = round(row["total"] - cheapest, 2)
    if result.incomplete:
        result.notes.append(
            f"{len(result.incomplete)} offer(s) are missing a cost and were not ranked — "
            f"a missing figure is not a zero one."
        )
    return result


def render(result: Comparison) -> str:
    """The comparison as text, with the arithmetic left visible to be checked."""
    money = f" {result.currency}" if result.currency else ""
    span = f" over {result.periods} periods" if result.periods > 1 else ""
    lines = [f"Total cost{span}, cheapest first{money}:"]

    for position, row in enumerate(result.ranked, start=1):
        breakdown = " + ".join(f"{key} {format_number(value)}" for key, value in row["parts"].items())
        delta = f"  (+{format_number(row['over_cheapest'])})" if row.get("over_cheapest") else ""
        lines.append(f"{position}. {row['name']}: {format_number(row['total'])}{delta}")
        if breakdown:
            lines.append(f"   = {breakdown}")
        if row["other"]:
            lines.append(f"   other: {_render_extras(row['other'])}")

    if result.incomplete:
        lines.append("")
        lines.append("Not ranked — a cost is missing, and a missing figure is not a zero one:")
        for row in result.incomplete:
            lines.append(f"- {row['name']}: {', '.join(row['missing'])}")
            if row["other"]:
                lines.append(f"   other: {_render_extras(row['other'])}")

    lines.append("")
    lines.append(
        "This is arithmetic only. Weighing the rest — location, seller, terms — is not "
        "something this decides; do that yourself and say why."
    )
    return "\n".join(lines)


def _render_extras(extras: dict) -> str:
    return ", ".join(f"{key} {value}" for key, value in list(extras.items())[:8])


@tool
async def compare_offers(offers: str, recurring: str = "", one_off: str = "", periods: int = 1) -> str:
    """Add up what each offer really costs and rank them, showing the arithmetic.

    Use this instead of totalling prices yourself — that is where invented numbers
    come from. It ranks on money only; weighing location, seller reputation or
    terms is your job, and the result says so.

    Offers missing one of the cost fields are set aside rather than ranked: a
    listing that does not state the deposit is not a listing without one.

    Args:
        offers: JSON array of objects, one per offer. Use "name" for the label and
            put each cost in its own field, e.g.
            [{"name":"Villa A","currency":"USD","price":800,"deposit":1600,"utilities":90,"rating":4.8}]
            Values may be written as on the page ("Rp 8.750.000") — they are parsed.
        recurring: comma-separated cost fields charged every period, e.g. "price,utilities".
        one_off: comma-separated costs charged once, e.g. "deposit,shipping,duty".
        periods: how many periods the recurring costs are counted for (months of
            stay, for instance). Default 1.
    """
    try:
        parsed = json.loads(offers)
    except json.JSONDecodeError as e:
        return f"[ERROR] offers must be a JSON array: {e}"
    if not isinstance(parsed, list):
        return "[ERROR] offers must be a JSON array of objects"

    try:
        result = compare(parsed, one_off=_split(one_off), recurring=_split(recurring), periods=periods)
    except CompareError as e:
        return f"[ERROR] {e}"

    return render(result)


# The output is bounded by MAX_OFFERS, and its value is being complete: a ranking
# cut off halfway is a different ranking. Exempt from the general output ceiling.
compare_offers.metadata = {**(compare_offers.metadata or {}), "output_max_chars": 0}
