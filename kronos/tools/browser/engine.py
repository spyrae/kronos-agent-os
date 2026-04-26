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
_lock = asyncio.Lock()


async def _ensure_browser():
    """Start browser if not running. Lazy initialization."""
    global _pw, _browser, _page

    if _page and not _page.is_closed():
        return _page

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright not installed. Run: pip install playwright && playwright install chromium"
        )

    async with _lock:
        if _page and not _page.is_closed():
            return _page

        if not _pw:
            _pw = await async_playwright().start()

        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )

        context = await _browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) Kronos-II/0.1",
            java_script_enabled=True,
        )
        _page = await context.new_page()
        log.info("Browser started (headless Chromium)")

    return _page


async def navigate(url: str, wait_until: str = "domcontentloaded") -> str:
    """Navigate to URL. Returns page title."""
    from kronos.tools.browser.security import is_url_safe

    safe, reason = is_url_safe(url)
    if not safe:
        return f"Navigation blocked: {reason}"

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
    """Get accessibility tree snapshot (compact, token-efficient).

    Returns structured text representation of the page,
    ~500 tokens vs ~5000 for raw HTML.
    """
    page = await _ensure_browser()
    try:
        # Use Playwright's accessibility snapshot
        tree = await page.accessibility.snapshot()
        if not tree:
            return "[Empty page — no accessibility tree]"
        return _format_a11y_tree(tree)
    except Exception as e:
        return f"Snapshot failed: {e}"


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
    global _pw, _browser, _page
    if _browser:
        await _browser.close()
        _browser = None
        _page = None
    if _pw:
        await _pw.stop()
        _pw = None
    log.info("Browser closed")


def _format_a11y_tree(node: dict, indent: int = 0) -> str:
    """Format accessibility tree into compact text representation."""
    lines = []
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    # Skip generic/container nodes without useful info
    if role in ("none", "generic", "presentation") and not name:
        pass
    else:
        prefix = "  " * indent
        parts = [role]
        if name:
            parts.append(f'"{name}"')
        if value:
            parts.append(f"[{value}]")
        lines.append(f"{prefix}{' '.join(parts)}")

    for child in node.get("children", []):
        lines.append(_format_a11y_tree(child, indent + 1))

    return "\n".join(line for line in lines if line)
