"""What a step can wait for, and how the poller finds out.

Each condition is a small deterministic question asked on a schedule: has the
time come, does this page still say that, has the number on it dropped below
what I care about, has the owner said anything. Nothing here calls a model — a
condition that costs a model call every few minutes would make waiting the
expensive part, when waiting is supposed to be the cheap part.

The verdict carries a `detail` string, and that string reaches the woken step's
prompt. It is deliberately the *observation* ("price is now 8 750 000, below
9 000 000") rather than a bare "fired": the step's whole job is usually to react
to what changed, and re-deriving it would mean fetching the page twice.

Conditions are validated when the step is parked, not when it wakes. A bad regex
or a URL the egress policy forbids should be a refusal the agent can see and fix
in the same turn, not a failure discovered next Tuesday.
"""

import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("kronos.plan_conditions")

KIND_AT = "at"
KIND_MANUAL = "manual"
KIND_PAGE_MATCHES = "page_matches"
KIND_PAGE_NUMBER = "page_number"
KIND_REPLY = "reply"
KINDS = (KIND_AT, KIND_MANUAL, KIND_PAGE_MATCHES, KIND_PAGE_NUMBER, KIND_REPLY)

# A page condition that re-fetched every minute would be a small crawler pointed
# at one site for weeks. Five minutes is the floor, an hour the default.
MIN_INTERVAL_SECONDS = 300
DEFAULT_INTERVAL_SECONDS = 3600
# The cheap conditions read the local database, so they can look more often.
CHEAP_INTERVAL_SECONDS = 300
# Long enough that a manually released step never wakes on its own; the plan's
# own expiry is what ends it.
NEVER_SECONDS = 400 * 86400

MAX_PATTERN_LENGTH = 200
MAX_DETAIL_CHARS = 400

OP_BELOW = "below"
OP_ABOVE = "above"
OPS = (OP_BELOW, OP_ABOVE)


class ConditionError(Exception):
    """Raised when a condition is malformed — while it can still be corrected."""


@dataclass
class Verdict:
    fired: bool
    detail: str = ""
    next_check_at: float = 0.0
    notes: list[str] = field(default_factory=list)


def _interval(spec: dict, *, floor: int, default: int) -> int:
    raw = spec.get("every_seconds", default)
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError) as e:
        raise ConditionError(f"every_seconds must be a number, got {raw!r}") from e
    return max(floor, seconds)


def _url(spec: dict, *, tool: str) -> str:
    from kronos.security.egress import check_url

    url = str(spec.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ConditionError(f"{tool} needs a url starting with http:// or https://")
    # The same allowlist the fetch tools obey. Checked here so a forbidden host
    # is refused while the agent is still in the turn that asked for it.
    check_url(url, tool=tool)
    return url


def _pattern(spec: dict, *, want_group: bool) -> re.Pattern:
    raw = str(spec.get("pattern") or "")
    if not raw:
        raise ConditionError("this condition needs a pattern")
    if len(raw) > MAX_PATTERN_LENGTH:
        raise ConditionError(f"pattern is longer than {MAX_PATTERN_LENGTH} characters")
    try:
        compiled = re.compile(raw, re.IGNORECASE)
    except re.error as e:
        raise ConditionError(f"pattern is not a valid regular expression: {e}") from e
    if want_group and compiled.groups < 1:
        raise ConditionError("pattern must capture the number in a group, e.g. r'Rp ([\\d.,]+)'")
    return compiled


def normalize(spec: dict, *, now: float | None = None) -> tuple[dict, float]:
    """Validate a condition and say when it should first be looked at.

    Returns the cleaned spec and the initial wake time. Page conditions get
    wake time 0 — checked on the next cycle — because a price that is *already*
    below the threshold should not have to drop twice.
    """
    stamp = time.time() if now is None else now
    if not isinstance(spec, dict):
        raise ConditionError("a condition must be an object")
    kind = str(spec.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ConditionError(f"unknown condition '{kind}' (expected one of {', '.join(KINDS)})")

    if kind == KIND_AT:
        if "timestamp" in spec:
            try:
                when = float(spec["timestamp"])
            except (TypeError, ValueError) as e:
                raise ConditionError("timestamp must be a unix time") from e
        else:
            try:
                seconds = float(spec.get("seconds", 0))
            except (TypeError, ValueError) as e:
                raise ConditionError("seconds must be a number") from e
            if seconds <= 0:
                raise ConditionError("'at' needs seconds > 0, or an absolute timestamp")
            when = stamp + seconds
        return {"kind": KIND_AT, "timestamp": when}, when

    if kind == KIND_MANUAL:
        return {"kind": KIND_MANUAL, "note": str(spec.get("note") or "")[:200]}, stamp + NEVER_SECONDS

    if kind == KIND_REPLY:
        interval = _interval(spec, floor=CHEAP_INTERVAL_SECONDS, default=CHEAP_INTERVAL_SECONDS)
        return {"kind": KIND_REPLY, "every_seconds": interval}, 0.0

    if kind == KIND_PAGE_MATCHES:
        url = _url(spec, tool="plan_wait_page_matches")
        pattern = _pattern(spec, want_group=False)
        return {
            "kind": KIND_PAGE_MATCHES,
            "url": url,
            "pattern": pattern.pattern,
            "absent": bool(spec.get("absent", False)),
            "every_seconds": _interval(spec, floor=MIN_INTERVAL_SECONDS, default=DEFAULT_INTERVAL_SECONDS),
        }, 0.0

    url = _url(spec, tool="plan_wait_page_number")
    pattern = _pattern(spec, want_group=True)
    op = str(spec.get("op") or "").strip().lower()
    if op not in OPS:
        raise ConditionError(f"op must be one of {', '.join(OPS)}")
    try:
        value = float(spec["value"])
    except (KeyError, TypeError, ValueError) as e:
        raise ConditionError("page_number needs a numeric value to compare against") from e
    return {
        "kind": KIND_PAGE_NUMBER,
        "url": url,
        "pattern": pattern.pattern,
        "op": op,
        "value": value,
        "every_seconds": _interval(spec, floor=MIN_INTERVAL_SECONDS, default=DEFAULT_INTERVAL_SECONDS),
    }, 0.0


def describe(spec: dict) -> str:
    """One line an owner can read in a list of plans."""
    kind = spec.get("kind")
    if kind == KIND_AT:
        return f"until {time.strftime('%Y-%m-%d %H:%M', time.localtime(spec.get('timestamp', 0)))}"
    if kind == KIND_MANUAL:
        note = spec.get("note")
        return f"for you to resume it{f' ({note})' if note else ''}"
    if kind == KIND_REPLY:
        return "for your reply"
    if kind == KIND_PAGE_MATCHES:
        verb = "stops matching" if spec.get("absent") else "matches"
        return f"until {spec.get('url', '')} {verb} /{spec.get('pattern', '')}/"
    if kind == KIND_PAGE_NUMBER:
        return f"until the number on {spec.get('url', '')} is {spec.get('op')} {format_number(spec.get('value', 0))}"
    return "for something unrecognised"


def parse_number(text: str) -> float | None:
    """Read a price the way a page writes one.

    Handles 1.234.567 / 1,234,567 / 8750000 / 1 234,56 — thousands separators and
    a decimal comma are both normal on the sites this exists for, and a wrong
    reading here is a notification that never comes or one that comes falsely.
    """
    cleaned = re.sub(r"[^\d.,]", "", text or "")
    if not cleaned:
        return None
    # The last separator is a decimal point only when 1–2 digits follow it.
    match = re.search(r"[.,](\d{1,2})$", cleaned)
    if match and len(re.sub(r"\D", "", cleaned)) > len(match.group(1)):
        whole = re.sub(r"\D", "", cleaned[: match.start()])
        try:
            return float(f"{whole}.{match.group(1)}")
        except ValueError:
            return None
    digits = re.sub(r"\D", "", cleaned)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def format_number(value: float) -> str:
    """A price a person can read. `%g` would turn 8750000 into 8.75e+06."""
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


async def _page_text(url: str, *, tool: str) -> tuple[str, list[str]]:
    from kronos.security.egress import check_url
    from kronos.tools.acquire import fetch_tiered, html_to_text

    # Re-checked at wake time: the allowlist can be tightened while a plan waits,
    # and a plan is not a way around the policy that is in force now.
    check_url(url, tool=tool)
    _, raw, notes = await fetch_tiered(url)
    text = html_to_text(raw) if "<" in raw[:2000] else raw
    return text, notes


async def _owner_spoke_since(plan: dict, since: float) -> bool:
    """Whether a turn started in the owner's own thread after the step parked.

    Plan steps run in their own thread (``plan:<id>``), so the agent's own work
    cannot be mistaken for the owner speaking.
    """
    thread_id = str(plan.get("thread_id") or "")
    if not thread_id:
        return False

    from kronos.config import settings
    from kronos.session import SessionStore

    store = SessionStore(settings.db_path, agent_name=settings.agent_name)
    turns = await store.list_turns(thread_id=thread_id, limit=5)
    for turn in turns:
        started = turn.get("started_at")
        if started and _as_epoch(started) > since:
            return True
    return False


def _as_epoch(value) -> float:
    """SQLite CURRENT_TIMESTAMP is a UTC string; turn it into seconds."""
    if isinstance(value, int | float):
        return float(value)
    from datetime import UTC, datetime

    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    log.debug("Cannot read turn timestamp %r", value)
    return 0.0


async def evaluate(spec: dict, *, step: dict, plan: dict, now: float | None = None) -> Verdict:
    """Ask the condition whether it is time. Never raises for a network failure.

    A page that cannot be fetched is *not* a fired condition and not a failure
    either — it is a check that did not happen, and the next one may work. That
    distinction is the difference between "the price dropped" and "the site was
    down", which must never be confused.
    """
    stamp = time.time() if now is None else now
    kind = spec.get("kind")

    if kind == KIND_AT:
        when = float(spec.get("timestamp") or 0)
        if stamp >= when:
            return Verdict(True, "the time you set has come")
        return Verdict(False, next_check_at=when)

    if kind == KIND_MANUAL:
        return Verdict(False, next_check_at=stamp + NEVER_SECONDS)

    if kind == KIND_REPLY:
        interval = int(spec.get("every_seconds") or CHEAP_INTERVAL_SECONDS)
        try:
            spoke = await _owner_spoke_since(plan, float(step.get("parked_at") or 0))
        except Exception as e:
            log.warning("Reply check failed for step %s: %s", step.get("id"), e)
            return Verdict(False, next_check_at=stamp + interval, notes=[f"could not check for a reply: {e}"])
        if spoke:
            return Verdict(True, "you replied in the chat this plan came from")
        return Verdict(False, next_check_at=stamp + interval)

    if kind in (KIND_PAGE_MATCHES, KIND_PAGE_NUMBER):
        interval = int(spec.get("every_seconds") or DEFAULT_INTERVAL_SECONDS)
        url = str(spec.get("url") or "")
        try:
            text, notes = await _page_text(url, tool=f"plan_{kind}")
        except Exception as e:
            log.info("Condition check for step %s could not read %s: %s", step.get("id"), url, e)
            return Verdict(False, next_check_at=stamp + interval, notes=[f"could not read {url}: {e}"])

        pattern = re.compile(str(spec.get("pattern") or ""), re.IGNORECASE)
        match = pattern.search(text)

        if kind == KIND_PAGE_MATCHES:
            present = match is not None
            want_absent = bool(spec.get("absent"))
            if present != want_absent:
                seen = match.group(0)[:MAX_DETAIL_CHARS] if match else ""
                detail = f"{url} no longer matches /{pattern.pattern}/" if want_absent else f"{url} now shows: {seen}"
                return Verdict(True, detail, notes=notes)
            return Verdict(False, next_check_at=stamp + interval, notes=notes)

        if match is None:
            # The pattern is how the number is found; without it there is nothing
            # to compare, and guessing would be worse than waiting.
            return Verdict(
                False,
                next_check_at=stamp + interval,
                notes=notes + [f"pattern /{pattern.pattern}/ did not match {url}"],
            )
        number = parse_number(match.group(1))
        if number is None:
            return Verdict(
                False,
                next_check_at=stamp + interval,
                notes=notes + [f"could not read a number from {match.group(1)!r}"],
            )
        target = float(spec.get("value") or 0)
        fired = number < target if spec.get("op") == OP_BELOW else number > target
        if fired:
            detail = f"the number on {url} is {format_number(number)}, {spec.get('op')} {format_number(target)}"
            return Verdict(True, detail, notes=notes)
        return Verdict(False, next_check_at=stamp + interval, notes=notes)

    log.warning("Step %s waits on an unrecognised condition %r", step.get("id"), kind)
    return Verdict(False, next_check_at=stamp + NEVER_SECONDS, notes=[f"unrecognised condition {kind!r}"])
