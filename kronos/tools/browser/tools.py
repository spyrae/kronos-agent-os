"""Browser LangChain tools — registered with the agent's toolset.

These are async tools wrapping the browser engine.
Available to supervisor and sub-agents.
"""

import base64
import logging

from langchain_core.tools import BaseTool, tool

from kronos.tools.browser import engine

log = logging.getLogger("kronos.tools.browser")


@tool
async def browser_navigate(url: str) -> str:
    """Navigate the browser to a URL. Returns page title and status.
    Use this to open web pages for reading, interacting, or taking screenshots.

    Args:
        url: The URL to navigate to (must be http:// or https://)
    """
    return await engine.navigate(url)


@tool
async def browser_snapshot() -> str:
    """Get an accessibility tree snapshot of the current page.
    This is a compact, token-efficient representation of the page content
    and interactive elements. Much cheaper than a screenshot for text content.
    Use this first before taking a screenshot.
    """
    return await engine.snapshot()


@tool
async def browser_screenshot() -> str:
    """Take a screenshot of the current page. Returns base64-encoded PNG.
    Use this only when you need to see visual layout, images, or charts.
    For text content, prefer browser_snapshot() instead.
    """
    png_bytes = await engine.screenshot()
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"[Screenshot captured: {len(png_bytes)} bytes, base64 encoded]\n{b64[:100]}..."


@tool
async def browser_click(selector: str) -> str:
    """Click an element on the page by CSS selector.

    Args:
        selector: CSS selector for the element to click (e.g. 'button.submit', '#login-btn')
    """
    return await engine.click(selector)


@tool
async def browser_type(selector: str, text: str) -> str:
    """Type text into an input field on the page.

    Args:
        selector: CSS selector for the input element
        text: Text to type into the field
    """
    return await engine.type_text(selector, text)


@tool
async def browser_evaluate(js_code: str) -> str:
    """Execute JavaScript code on the current page and return the result.
    Use for extracting specific data, scrolling, or page manipulation.

    Args:
        js_code: JavaScript code to execute (e.g. 'document.title')
    """
    return await engine.evaluate(js_code)


def get_browser_tools() -> list[BaseTool]:
    """Get all browser tools. Returns empty list if playwright not available."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        log.info("Browser tools disabled: playwright not installed")
        return []

    return [
        browser_navigate,
        browser_snapshot,
        browser_screenshot,
        browser_click,
        browser_type,
        browser_evaluate,
    ]
