"""A signed-in browser profile has to survive the next call.

Every function in the engine asks for the browser with no profile argument, so
if that meant "the default profile" instead of "whatever is open", opening a
site session and then navigating would quietly throw the session away — and the
agent would scrape a login wall believing it was signed in.
"""

import pytest

from kronos.tools.browser import engine


class _FakePage:
    def __init__(self, closed: bool = False):
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


@pytest.fixture(autouse=True)
def live_profile(monkeypatch):
    """A browser already open on the owner's Airbnb profile."""
    page = _FakePage()
    monkeypatch.setattr(engine, "_page", page)
    monkeypatch.setattr(engine, "_profile_dir", "/profiles/airbnb")
    monkeypatch.setattr(engine, "_browser", object())

    closed: list[bool] = []

    async def track_close():
        closed.append(True)

    monkeypatch.setattr(engine, "close", track_close)
    return page, closed


async def test_navigating_keeps_the_open_profile(live_profile):
    page, closed = live_profile

    assert await engine._ensure_browser() is page
    assert not closed, "a plain navigation must not tear down the signed-in session"


async def test_asking_for_the_same_profile_reuses_it(live_profile):
    page, closed = live_profile

    assert await engine._ensure_browser(profile_dir="/profiles/airbnb") is page
    assert not closed


async def test_another_profile_restarts_the_browser(live_profile, monkeypatch):
    """Two profiles in one process would silently share one context."""
    _, closed = live_profile
    monkeypatch.setattr(engine, "_pw", None)

    with pytest.raises(RuntimeError, match="playwright not installed"):
        await engine._ensure_browser(profile_dir="/profiles/booking")

    assert closed == [True]


async def test_a_dead_page_does_not_silently_reopen_the_profile(live_profile, monkeypatch):
    """Better to report the session expired than to guess it is still there."""
    monkeypatch.setattr(engine, "_page", _FakePage(closed=True))
    monkeypatch.setattr(engine, "_pw", None)

    with pytest.raises(RuntimeError, match="playwright not installed"):
        await engine._ensure_browser()
