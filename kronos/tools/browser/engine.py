"""Browser engine — Playwright wrapper for headless Chrome.

Manages browser lifecycle (lazy start, auto-cleanup).
Provides low-level methods used by tool functions.
"""

import asyncio
import logging

log = logging.getLogger("kronos.tools.browser.engine")

# Lazy import — playwright is optional
_pw = None
_browser = None
_page = None
_profile_dir = None
_lock = asyncio.Lock()


async def _ensure_browser(profile_dir: str | None = None):
    """Start browser if not running. Lazy initialization.

    With ``profile_dir`` the browser opens a persistent profile instead of a
    throwaway context, which is how a session the owner created by hand — logged
    in once, second factor already passed — is reused without storing anything
    secret. Switching to a *different* profile restarts the browser: two profiles
    in one process would silently share whichever context happened to be open.

    ``None`` means "whatever is already open", not "the default profile". Every
    other function here calls it that way, so opening a site session and then
    navigating has to keep the session — otherwise the second call would tear the
    signed-in profile down and the first one would have achieved nothing. When no
    page is live, ``None`` does start a throwaway browser: a session that died is
    then reported as expired, which is the safe direction to fail in.
    """
    global _pw, _browser, _page, _profile_dir

    if _page and not _page.is_closed() and profile_dir in (None, _profile_dir):
        return _page
    if _page and profile_dir is not None and profile_dir != _profile_dir:
        log.info("Switching browser profile: %s -> %s", _profile_dir or "none", profile_dir or "none")
        await close()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright not installed. Run: pip install playwright && playwright install chromium")

    async with _lock:
        if _page and not _page.is_closed():
            return _page

        if not _pw:
            _pw = await async_playwright().start()

        launch_args = [
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
        ]

        if profile_dir:
            # A persistent context *is* the browser: cookies, storage and the
            # signed-in session live in the directory, so nothing is kept here.
            context = await _pw.chromium.launch_persistent_context(
                profile_dir,
                headless=True,
                args=launch_args,
                viewport={"width": 1280, "height": 720},
            )
            _browser = context.browser
            _page = context.pages[0] if context.pages else await context.new_page()
            log.info("Browser started with profile: %s", profile_dir)
        else:
            _browser = await _pw.chromium.launch(headless=True, args=launch_args)
            context = await _browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) Kronos-II/0.1",
                java_script_enabled=True,
            )
            _page = await context.new_page()
            log.info("Browser started (headless Chromium)")
        _profile_dir = profile_dir

    return _page


async def navigate(url: str, wait_until: str = "domcontentloaded") -> str:
    """Navigate to URL. Returns page title."""
    from kronos.security.egress import EgressBlockedError, check_url
    from kronos.tools.browser.security import is_url_safe

    safe, reason = is_url_safe(url)
    if not safe:
        return f"Navigation blocked: {reason}"

    # is_url_safe covers what must never be reachable (schemes, private ranges);
    # the policy covers what this deployment chose to allow.
    try:
        check_url(url, tool="browser_navigate")
    except EgressBlockedError as e:
        return f"Navigation blocked: {e}"

    page = await _ensure_browser()
    try:
        response = await page.goto(url, wait_until=wait_until, timeout=30000)
        status = response.status if response else "unknown"
        title = await page.title()
        log.info("Navigated to %s (status=%s)", url[:80], status)
        return f"Navigated to: {title} (status {status})"
    except Exception as e:
        return f"Navigation failed: {e}"


async def snapshot() -> str:
    """A compact view of the page — roles and text, not markup.

    Current Playwright has no ``accessibility`` on Page — it was removed, and the
    failure was the quiet kind: the snapshot *returned* the error message, so the
    browser tier fed that sentence to the extractor and the signed-in check found
    no "log in" in it and said yes. ``aria_snapshot`` is the replacement and gives
    the same tree in YAML; visible text is the fallback for a page with no
    accessible structure worth printing.
    """
    page = await _ensure_browser()
    try:
        tree = await page.locator("body").aria_snapshot()
        if tree and tree.strip():
            return tree
    except Exception as e:
        log.debug("aria_snapshot unavailable (%s); falling back to visible text", e)

    try:
        text = await page.locator("body").inner_text(timeout=5000)
        return text.strip() or "[Empty page]"
    except Exception as e:
        return f"Snapshot failed: {e}"


CONTENT_SETTLE_MS = 4000


async def page_html() -> str:
    """The page's markup after scripts have run.

    What a JavaScript-rendered listing actually contains — the point of using a
    browser at all. Extraction needs the markup, not a compact tree: a price in
    an attribute or a data- field is invisible in an accessibility snapshot.

    A single-page app keeps navigating after DOMContentLoaded, and reading it
    mid-flight fails with "the page is navigating and changing the content" —
    which is what a marketplace does on every visit. So: wait for the network to
    go quiet, and if the read still lands in a navigation, wait and take it once
    more rather than reporting the race as an unreadable page.
    """
    page = await _ensure_browser()
    try:
        await page.wait_for_load_state("networkidle", timeout=CONTENT_SETTLE_MS)
    except Exception as e:
        log.debug("Page never went quiet (%s); reading it as it is", e)

    for attempt in (1, 2):
        try:
            return await page.content()
        except Exception as e:
            if attempt == 2:
                return f"Could not read the page: {e}"
            log.debug("Content read raced with a navigation; retrying once")
            try:
                await page.wait_for_timeout(CONTENT_SETTLE_MS // 2)
            except Exception:  # pragma: no cover - page went away entirely
                pass
    return "Could not read the page"


async def screenshot() -> bytes:
    """Take PNG screenshot of visible viewport."""
    page = await _ensure_browser()
    return await page.screenshot(type="png", full_page=False)


async def click(selector: str) -> str:
    """Click an element by CSS selector."""
    page = await _ensure_browser()
    try:
        await page.click(selector, timeout=5000)
        return f"Clicked: {selector}"
    except Exception as e:
        return f"Click failed on '{selector}': {e}"


async def type_text(selector: str, text: str) -> str:
    """Type text into an input field."""
    page = await _ensure_browser()
    try:
        await page.fill(selector, text, timeout=5000)
        return f"Typed into {selector}: {text[:50]}"
    except Exception as e:
        return f"Type failed on '{selector}': {e}"


async def evaluate(js_code: str) -> str:
    """Execute JavaScript and return result."""
    page = await _ensure_browser()
    try:
        result = await page.evaluate(js_code)
        return str(result)[:2000]
    except Exception as e:
        return f"JS evaluation failed: {e}"


async def get_current_url() -> str:
    """Get current page URL."""
    page = await _ensure_browser()
    return page.url


async def close():
    """Close browser and cleanup."""
    global _pw, _browser, _page, _profile_dir
    if _page and not _page.is_closed():
        # A persistent context owns the profile directory; closing the context
        # is what flushes the session back to disk for the next run.
        try:
            await _page.context.close()
        except Exception as e:  # pragma: no cover - best effort on shutdown
            log.debug("Closing browser context failed: %s", e)
    if _browser:
        try:
            await _browser.close()
        except Exception as e:  # pragma: no cover
            log.debug("Closing browser failed: %s", e)
        _browser = None
    _page = None
    _profile_dir = None
    if _pw:
        await _pw.stop()
        _pw = None
    log.info("Browser closed")
