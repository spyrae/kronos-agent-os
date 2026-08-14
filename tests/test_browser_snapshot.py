"""Reading a page without the API Playwright removed.

`page.accessibility` is gone from current Playwright, and the way it failed was
the worst kind: `snapshot()` returned the string "Snapshot failed: 'Page' object
has no attribute 'accessibility'", which is a *successful* return. So the browser
acquisition tier handed that sentence to the extractor as page content, and
`_looks_signed_in` — which searches a snapshot for the word "log in" — found none
and answered "signed in" forever, on a session that may well have expired.

Found by pointing the deployed agent at a real marketplace.
"""

import pytest

from kronos.tools.browser import engine


class FakeLocator:
    def __init__(self, aria: str | Exception = "", text: str | Exception = ""):
        self._aria = aria
        self._text = text

    async def aria_snapshot(self):
        if isinstance(self._aria, Exception):
            raise self._aria
        return self._aria

    async def inner_text(self, timeout: int = 0):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class FakePage:
    def __init__(self, locator: FakeLocator, html: str | Exception = "<html></html>"):
        self._locator = locator
        self._html = html

    def locator(self, selector: str):
        assert selector == "body"
        return self._locator

    async def content(self):
        if isinstance(self._html, Exception):
            raise self._html
        return self._html


@pytest.fixture
def page(monkeypatch):
    def install(fake: FakePage) -> FakePage:
        async def fake_browser(profile_dir=None):
            return fake

        monkeypatch.setattr(engine, "_ensure_browser", fake_browser)
        return fake

    return install


async def test_the_snapshot_uses_the_api_that_exists(page):
    page(FakePage(FakeLocator(aria='- heading "Sold out" [level=1]')))

    assert "Sold out" in await engine.snapshot()


async def test_a_page_with_no_aria_structure_falls_back_to_its_text(page):
    page(FakePage(FakeLocator(aria=RuntimeError("no aria"), text="Log in to continue")))

    assert await engine.snapshot() == "Log in to continue"


async def test_an_empty_aria_tree_is_not_treated_as_the_answer(page):
    page(FakePage(FakeLocator(aria="   ", text="Some visible text")))

    assert await engine.snapshot() == "Some visible text"


async def test_a_page_that_cannot_be_read_says_so_rather_than_returning_prose(page):
    """The bug: an error sentence returned as content reads as a signed-in page."""
    page(FakePage(FakeLocator(aria=RuntimeError("gone"), text=RuntimeError("also gone"))))

    result = await engine.snapshot()

    assert result.startswith("Snapshot failed")


async def test_a_blank_page_is_named_blank(page):
    page(FakePage(FakeLocator(aria="", text="")))

    assert await engine.snapshot() == "[Empty page]"


async def test_the_browser_tier_returns_markup_not_a_summary(page):
    """Extraction needs the markup: a price in an attribute is invisible in a tree."""
    page(FakePage(FakeLocator(aria="- text: Rp 8.750.000"), html='<div data-price="8750000">Rp 8.750.000</div>'))

    html = await engine.page_html()

    assert 'data-price="8750000"' in html


async def test_an_unreadable_page_reports_it(page):
    page(FakePage(FakeLocator(), html=RuntimeError("navigation lost")))

    assert "Could not read the page" in await engine.page_html()


def test_the_removed_api_is_not_referenced_anywhere():
    """A guard against reintroducing it: it exists in no current Playwright."""
    from pathlib import Path

    source = Path(engine.__file__).read_text(encoding="utf-8")

    assert "accessibility.snapshot(" not in source


# --- the browser tier's own output is not trusted just because a browser ran ---


class NavigatingPage(FakePage):
    """A single-page app that is still moving the first time it is read."""

    def __init__(self, html: str, failures: int = 1):
        super().__init__(FakeLocator(), html)
        self._failures = failures
        self.reads = 0

    async def wait_for_load_state(self, state: str, timeout: int = 0):
        return None

    async def wait_for_timeout(self, ms: int):
        return None

    async def content(self):
        self.reads += 1
        if self.reads <= self._failures:
            raise RuntimeError("Page.content: Unable to retrieve content because the page is navigating")
        return self._html


async def test_a_read_that_raced_a_navigation_is_retried(page):
    """Every marketplace does this; giving up on the first race loses the page."""
    fake = page(NavigatingPage("<html><body>Rp 8.750.000</body></html>"))

    html = await engine.page_html()

    assert "8.750.000" in html
    assert fake.reads == 2


async def test_a_page_that_never_settles_is_reported_not_guessed(page):
    fake = page(NavigatingPage("<html/>", failures=5))

    assert "Could not read the page" in await engine.page_html()
    assert fake.reads == 2, "retried once, then said so"


async def test_the_browser_tier_refuses_its_own_error_sentence(monkeypatch):
    """The bug this closes: an error string counted as a successful fetch."""
    from kronos.tools import acquire

    async def fake_navigate(url, wait_until="domcontentloaded"):
        return "ok"

    async def fake_html():
        return "Could not read the page: navigating"

    monkeypatch.setattr(engine, "navigate", fake_navigate)
    monkeypatch.setattr(engine, "page_html", fake_html)

    with pytest.raises(acquire.FetchBlockedError, match="no usable content"):
        await acquire.fetch_browser("https://shop.test/item")


async def test_the_browser_tier_refuses_markup_with_no_words_in_it(monkeypatch):
    """158 KB of shell and zero characters of text is not a page that loaded."""
    from kronos.tools import acquire

    async def fake_navigate(url, wait_until="domcontentloaded"):
        return "ok"

    async def fake_html():
        return "<html><head>" + ("<script>x=1;</script>" * 3000) + "</head><body></body></html>"

    monkeypatch.setattr(engine, "navigate", fake_navigate)
    monkeypatch.setattr(engine, "page_html", fake_html)

    with pytest.raises(acquire.FetchBlockedError, match="no readable content"):
        await acquire.fetch_browser("https://shop.test/item")


async def test_a_real_page_through_the_browser_tier_is_accepted(monkeypatch):
    from kronos.tools import acquire

    async def fake_navigate(url, wait_until="domcontentloaded"):
        return "ok"

    async def fake_html():
        return "<html><body><h1>ROG Ally X</h1><p>Rp 8.750.000 — ready stock</p></body></html>"

    monkeypatch.setattr(engine, "navigate", fake_navigate)
    monkeypatch.setattr(engine, "page_html", fake_html)

    status, body = await acquire.fetch_browser("https://shop.test/item")

    assert status == 200
    assert "8.750.000" in body
