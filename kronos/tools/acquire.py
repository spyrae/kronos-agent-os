"""Getting a page, and turning it into fields (gap #2).

The agent could already search the web and drive a stateful browser, but it had
no way to say "give me the content of this URL". For the sites these tasks
actually target — marketplaces, listings, booking sites — a plain GET returns 403
and a full browser session is far too heavy to run for twenty product cards.

Two tools, both read-only so they qualify for the engine's parallel path (which
is the whole point: checking two marketplaces should cost the slower of the two,
not the sum).

**Tiering, not maximalism.** Escalation is by evidence, never by default: a plain
fetch first, a stealth fetch only when the response looks like a block, a full
browser only when stealth also fails. Starting at the top would make every fetch
slow and would burn the expensive backend on pages that never needed it. The tier
that worked is reported back, because "this came from a plain GET" and "this
needed a fingerprint-spoofing browser" are different facts about a source.

**The backends are not dependencies.** Scrapling and CloakBrowser live outside
KAOS; a deployment may have neither. A missing backend is a reported, skipped
tier — never an import error at startup and never a silent empty result. Wire one
in with `STEALTH_FETCH_COMMAND` (a shell template containing `{url}`), which also
keeps machine-specific paths out of this repo.

**Everything fetched is untrusted.** A product page telling the agent to message
someone is the textbook injection, and this is the tool that would carry it.
"""

import asyncio
import json
import logging
import re
import shlex
from dataclasses import dataclass

from langchain_core.tools import tool

from kronos.config import settings
from kronos.security.untrusted import frame_external, mark_untrusted

log = logging.getLogger("kronos.tools.acquire")

FETCH_TIMEOUT_SECONDS = 45
STEALTH_TIMEOUT_SECONDS = 90

# How much of a page reaches the model. A marketplace page is mostly navigation;
# past this the useful part is already in.
MAX_CONTENT_CHARS = 20_000

# Response shapes that mean "you were blocked", not "the page is like this".
BLOCK_STATUS_CODES = {401, 403, 405, 406, 409, 429, 503}
BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "verify you are human",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
    "px-captcha",
    "datadome",
    "access denied",
    "request blocked",
    "unusual traffic",
)

# A page is judged by how much of it is readable, relative to how much was sent.
# An absolute floor does not work: example.com is a real page with 180 characters
# of text, while Tokopedia sends 105 KB of markup wrapped around 234. Only the
# ratio separates "short page" from "shell that renders client-side", and only
# above a size where the ratio means anything.
SHELL_HTML_CHARS = 20_000
SHELL_TEXT_RATIO = 0.01

# What a stealth backend must return before it counts as having fetched anything.
MIN_BACKEND_OUTPUT_CHARS = 200

TIER_PLAIN = "plain"
TIER_STEALTH = "stealth"
TIER_BROWSER = "browser"


class FetchBlockedError(Exception):
    """The response was a block page, not the content."""


def html_to_text(html: str) -> str:
    """Readable text from a page, without pulling in a parser dependency.

    Crude by design: script and style go, tags collapse, whitespace normalises.
    Enough to feed a model or an extractor; not a replacement for a real parser
    when a caller needs structure (that is what `selector` and Scrapling are for).
    """
    without_scripts = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    unescaped = (
        without_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    collapsed = re.sub(r"[ \t\xa0]+", " ", unescaped)
    # Trim each line before collapsing blank runs: stripping tags leaves stray
    # spaces hugging the newlines, and they survive every later pass otherwise.
    lines = [line.strip() for line in collapsed.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def looks_blocked(status: int, body: str) -> bool:
    """Whether this response failed to deliver usable content.

    Covers two different failures that call for the same answer — escalate:
    a refusal (a status code or a challenge page) and a page that technically
    arrived but says nothing (a single-page app that renders client-side).

    Both judgements are made on the **readable text**, not the raw HTML, and
    that distinction was not theoretical. Measured against the real sites these
    tools target:

        Tokopedia  plain 105 KB of HTML →   234 characters of text
        Shopee     plain 157 KB of HTML →     0 characters of text
        Shopee     stealth 480 KB       → 12871 characters of text

    All six responses contain the word "captcha" — as a string constant inside a
    JavaScript bundle, not as a challenge shown to anyone. Scanning raw HTML for
    markers rejected every one of them, including the stealth fetch that had
    actually worked. Scanning text keeps the marker check meaningful and turns
    "arrived but empty" into the escalation signal it should be.
    """
    if status in BLOCK_STATUS_CODES:
        return True
    if 400 <= status < 500:
        # A genuine 404 or 410 is an answer. Escalating would fetch the same
        # page more expensively and report the same thing.
        return False

    text = html_to_text(body) if "<" in body[:2000] else body
    lowered = text.lower()
    if any(marker in lowered for marker in BLOCK_MARKERS):
        return True
    if not text.strip():
        return True
    # Markup without content: a shell the browser would have filled in.
    return len(body) > SHELL_HTML_CHARS and len(text) < len(body) * SHELL_TEXT_RATIO


async def fetch_plain(url: str) -> tuple[int, str]:
    """An ordinary GET. Cheap, and enough for most of the web."""
    import aiohttp

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            return response.status, await response.text(errors="replace")


def stealth_command(url: str) -> list[str] | None:
    """The configured stealth fetcher, or None when none is wired in.

    A command template rather than an import: the working stack (CloakBrowser +
    Scrapling) lives outside this repo and its path differs per machine, and a
    subprocess boundary also keeps a heavyweight browser out of the agent's own
    process.
    """
    template = (settings.stealth_fetch_command or "").strip()
    if not template:
        return None
    if "{url}" not in template:
        log.warning("STEALTH_FETCH_COMMAND has no {url} placeholder; ignoring it")
        return None
    return [part.replace("{url}", url) for part in shlex.split(template)]


async def fetch_stealth(url: str) -> tuple[int, str]:
    """Fetch through the configured stealth backend."""
    command = stealth_command(url)
    if command is None:
        raise FetchBlockedError("no stealth backend configured (set STEALTH_FETCH_COMMAND)")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=STEALTH_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        raise FetchBlockedError(f"stealth fetch timed out after {STEALTH_TIMEOUT_SECONDS}s") from None

    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise FetchBlockedError(f"stealth backend failed: {detail or 'no output'}")

    body = (stdout or b"").decode("utf-8", "replace")
    # Exit code 0 is not proof of a page. A misconfigured backend happily prints
    # its own advice ("install X for CSS extraction") and exits clean — found by
    # running this against a real CloakBrowser install. Treat a body that does
    # not look like content as a failed tier so escalation continues instead of
    # handing the model a 37-character non-answer as if it were the listing.
    if not _looks_like_a_page(body) or looks_blocked(200, body):
        raise FetchBlockedError(f"stealth backend returned no usable content: {body.strip()[:200] or 'empty output'}")
    return 200, body


def _looks_like_a_page(body: str) -> bool:
    """The backend promised a page; a one-line message is not one.

    Separate from `looks_blocked` on purpose: that judges web pages, this judges
    whether a subprocess honoured its contract. A misconfigured backend prints
    advice and exits 0, and 36 characters of "install X for CSS extraction" is
    indistinguishable from a short page by any rule about pages.
    """
    return "<" in body[:2000] or len(body.strip()) >= MIN_BACKEND_OUTPUT_CHARS


async def fetch_browser(url: str) -> tuple[int, str]:
    """Last resort: the real browser this agent already drives."""
    try:
        from kronos.tools.browser import engine
    except Exception as e:  # pragma: no cover - optional extra
        raise FetchBlockedError(f"browser backend unavailable: {e}") from e

    await engine.navigate(url)
    # The page's HTML, not a snapshot: every caller here runs html_to_text over
    # what comes back, and a compact accessibility tree is the wrong input for
    # extraction — it drops prices that live in attributes and markup.
    body = await engine.page_html()

    # Validated like every other tier. A browser can hand back its own error
    # sentence, or a shell whose 158 KB of markup carry no words at all, and
    # calling either a successful fetch is how a marketplace that refused to be
    # read becomes an empty answer with no explanation.
    if not _looks_like_a_page(body):
        raise FetchBlockedError(f"browser returned no usable content: {body.strip()[:200] or 'empty output'}")
    if looks_blocked(200, body):
        raise FetchBlockedError("browser loaded the page but it has no readable content (still a shell, or blocked)")
    return 200, body


async def fetch_tiered(url: str) -> tuple[str, str, list[str]]:
    """Try the cheap way first. Returns (tier, content, notes-on-what-failed)."""
    notes: list[str] = []

    try:
        status, body = await fetch_plain(url)
        if not looks_blocked(status, body):
            return TIER_PLAIN, body, notes
        notes.append(f"plain fetch looked blocked (HTTP {status})")
    except Exception as e:
        notes.append(f"plain fetch failed: {e}")

    try:
        _, body = await fetch_stealth(url)
        return TIER_STEALTH, body, notes
    except FetchBlockedError as e:
        notes.append(str(e))
    except Exception as e:
        notes.append(f"stealth fetch failed: {e}")

    try:
        _, body = await fetch_browser(url)
        return TIER_BROWSER, body, notes
    except FetchBlockedError as e:
        notes.append(str(e))
    except Exception as e:
        notes.append(f"browser fetch failed: {e}")

    raise FetchBlockedError("; ".join(notes) or "no backend could fetch this page")


# ── Tier health ───────────────────────────────────────────────────────────────
#
# Every tier here degrades quietly. A stealth backend uninstalled by a host
# rebuild, a browser whose API moved under it, a plain fetch that started
# meeting a CDN — none of them raise at startup, none fail a test, and all of
# them turn into "that marketplace can't be read" weeks later, attributed to
# the marketplace. Three of this module's real bugs were found by running it
# against the live web and none by the suite, because a test that fakes the
# boundary cannot notice the boundary changing.

TIER_OK = "ok"
TIER_BROKEN = "broken"
TIER_OFF = "off"

# Deliberately not a marketplace. The question here is "does our machinery
# still work", and a marketplace answers a different one — "is that site in a
# good mood today" — which changes for reasons outside this repo and on a
# schedule nobody controls. A checker that cries wolf whenever Shopee tightens
# its defences is a checker people learn to ignore, and then it is worth less
# than nothing. example.com exists for exactly this and changes for nobody.
SMOKE_URL = "https://example.com"


@dataclass(frozen=True)
class TierHealth:
    """What one acquisition tier can do right now."""

    tier: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == TIER_OK


async def check_tier_health(url: str = SMOKE_URL) -> list[TierHealth]:
    """Probe each tier against a page none of them has any reason to be refused.

    Three outcomes per tier, and the third is the one worth having: `off` means
    a backend this deployment never installed, which is a documented choice and
    not a fault. Collapsing it into `broken` would page somebody about a stealth
    browser they deliberately never wanted, and an alert that is usually wrong
    gets muted — taking the real ones with it.
    """
    from kronos.security.egress import check_url

    try:
        check_url(url, tool="acquire-health")
    except Exception as e:
        # Bypassing the policy to run a health check would be a strange place
        # to put a hole. Report instead: unrunnable is not the same as broken.
        return [TierHealth(tier, TIER_OFF, f"cannot probe: {e}") for tier in (TIER_PLAIN, TIER_STEALTH, TIER_BROWSER)]

    return [
        await _check_plain(url),
        await _check_stealth(url),
        await _check_browser(url),
    ]


async def _check_plain(url: str) -> TierHealth:
    try:
        status, body = await fetch_plain(url)
    except Exception as e:
        return TierHealth(TIER_PLAIN, TIER_BROKEN, f"{type(e).__name__}: {e}")
    if looks_blocked(status, body):
        return TierHealth(TIER_PLAIN, TIER_BROKEN, f"HTTP {status}, and the body reads as a block or a shell")
    return TierHealth(TIER_PLAIN, TIER_OK, f"HTTP {status}, {len(html_to_text(body))} characters of text")


async def _check_stealth(url: str) -> TierHealth:
    if stealth_command(url) is None:
        return TierHealth(TIER_STEALTH, TIER_OFF, "no STEALTH_FETCH_COMMAND configured")
    try:
        _, body = await fetch_stealth(url)
    except Exception as e:
        return TierHealth(TIER_STEALTH, TIER_BROKEN, str(e))
    return TierHealth(TIER_STEALTH, TIER_OK, f"{len(html_to_text(body))} characters of text")


async def _check_browser(url: str) -> TierHealth:
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        return TierHealth(TIER_BROWSER, TIER_OFF, "playwright is not installed (pip install -e '.[browser]')")

    from kronos.tools.browser import engine

    # Leave the process as we found it. If the agent already had a browser open
    # — a signed-in site session, most likely — closing it here would end that
    # session to prove an unrelated point.
    was_running = engine.is_running()
    try:
        _, body = await fetch_browser(url)
    except Exception as e:
        return TierHealth(TIER_BROWSER, TIER_BROKEN, str(e))
    finally:
        if not was_running:
            try:
                await engine.close()
            except Exception as e:  # pragma: no cover - best effort
                log.debug("Closing the probe's browser failed: %s", e)
    return TierHealth(TIER_BROWSER, TIER_OK, f"{len(html_to_text(body))} characters of text")


@tool
async def fetch_page(url: str, selector: str = "") -> str:
    """Fetch the readable content of a web page.

    Handles sites that block ordinary requests: tries a plain fetch first and
    escalates to a stealth fetcher and then a real browser only if the response
    looks like a block. Reports which method worked.

    Use this for one-off reads (a listing, a product card, an article). For a
    page you need to click through or fill in, use the browser_* tools instead.

    Args:
        url: Page to fetch (http:// or https://).
        selector: Optional CSS selector to keep only a part of the page.
    """
    from kronos.security.egress import check_url

    try:
        check_url(url, tool="fetch_page")
    except Exception as e:
        return f"[ERROR] {e}"

    try:
        tier, raw, notes = await fetch_tiered(url)
    except FetchBlockedError as e:
        return f"[ERROR] Could not fetch {url}: {e}"

    content = _select(raw, selector) if selector else raw
    text = html_to_text(content) if "<" in content[:2000] else content
    truncated = len(text) > MAX_CONTENT_CHARS
    if truncated:
        text = text[:MAX_CONTENT_CHARS]

    header = [f"Source: {url}", f"Fetched via: {tier}"]
    if notes:
        header.append("Escalated because: " + "; ".join(notes))
    if truncated:
        header.append(f"Truncated to {MAX_CONTENT_CHARS} characters")
    return "\n".join(header) + "\n\n" + text


def _select(html: str, selector: str) -> str:
    """Keep only the selected part, when a parser is available.

    Without one the whole page is returned rather than a wrong guess — a silently
    empty selection would read as "the page has nothing", which is a worse
    failure than too much text.
    """
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415 - optional
    except Exception:
        log.info("No HTML parser available; ignoring selector %r", selector)
        return html

    soup = BeautifulSoup(html, "html.parser")
    found = soup.select(selector)
    if not found:
        log.info("Selector %r matched nothing; returning the whole page", selector)
        return html
    return "\n".join(str(node) for node in found)


@tool
async def extract_structured(content: str, fields: str) -> str:
    """Pull named fields out of messy text into JSON.

    Use this to turn a fetched page into comparable data before reasoning about
    it — prices, dates, ratings, conditions. Anything the text does not state
    comes back as null; it is never guessed.

    Args:
        content: The text to read (e.g. the output of fetch_page).
        fields: Comma-separated field names, optionally with a hint in
            parentheses, e.g. "price (number, in local currency), seller_rating,
            shipping_days (integer), condition (new|used)".
    """
    wanted = [name.strip() for name in fields.split(",") if name.strip()]
    if not wanted:
        return '[ERROR] No fields requested. Pass something like "price, rating, seller".'
    if not content.strip():
        return "[ERROR] Nothing to extract from: content is empty."

    from langchain_core.messages import HumanMessage

    from kronos.llm import ModelTier, get_model

    instruction = (
        "Extract the requested fields from the text below.\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        f"{json.dumps([name.split('(')[0].strip() for name in wanted], ensure_ascii=False)}\n\n"
        "Field notes (type hints in parentheses):\n" + "\n".join(f"- {name}" for name in wanted) + "\n\nRules:\n"
        "- A field the text does not state is null. Never infer or estimate it.\n"
        "- Numbers as numbers, without currency symbols or thousands separators.\n"
        "- No commentary, no markdown fence — the JSON object alone.\n\n"
        # The content is data to read, not instructions to follow: a page saying
        # "ignore previous instructions" is exactly what this tool would carry.
         + frame_external(content[:MAX_CONTENT_CHARS], source="fetched page")
    )

    try:
        model = get_model(ModelTier.LITE)
        response = await model.ainvoke([HumanMessage(content=instruction)])
        raw = response.content if isinstance(response.content, str) else str(response.content)
    except Exception as e:
        return f"[ERROR] Extraction failed: {e}"

    parsed = _parse_json_object(raw)
    if parsed is None:
        return f"[ERROR] Extractor did not return JSON: {raw[:200]}"
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _parse_json_object(raw: str) -> dict | None:
    """Best-effort JSON out of a model reply that may carry a fence."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        braces = re.search(r"\{.*\}", text, re.DOTALL)
        if braces:
            text = braces.group(0)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Both read the outside world, so both are untrusted; neither changes anything,
# so both may run in the engine's parallel batch.
ACQUIRE_TOOLS = mark_untrusted([fetch_page, extract_structured], reason="web content")
