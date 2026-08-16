"""Noticing that a fetch tier stopped working.

Every tier degrades quietly: a stealth backend removed by a host rebuild, a
browser whose API moved under it, a plain fetch that started meeting a CDN. The
three real bugs in this module were all found by running it against the live web
and none by the suite — so what is tested here is not "does fetching work" (a
test that fakes the boundary cannot notice the boundary changing) but the
reporting around it: that a fault is distinguished from a deliberate absence,
and that probing does not damage what it probes.
"""

import pytest

from kronos.config import settings
from kronos.tools.acquire import (
    SMOKE_URL,
    TIER_BROKEN,
    TIER_BROWSER,
    TIER_OFF,
    TIER_OK,
    TIER_PLAIN,
    TIER_STEALTH,
    FetchBlockedError,
    check_tier_health,
)

REAL_PAGE = "<html><body><h1>Example Domain</h1><p>For use in documents.</p></body></html>"


@pytest.fixture(autouse=True)
def no_backends(monkeypatch):
    """Default to a host with nothing optional installed."""
    monkeypatch.setattr(settings, "stealth_fetch_command", "")
    return monkeypatch


def _by_tier(results):
    return {r.name: r for r in results}


def working_plain(monkeypatch):
    async def _fetch(url):
        return 200, REAL_PAGE

    monkeypatch.setattr("kronos.tools.acquire.fetch_plain", _fetch)


def _pretend_playwright(monkeypatch, installed: bool):
    """Decide the browser tier's availability, whichever way this host is set up.

    Pinned in both directions on purpose: the browser extra is optional, so a
    test that reads the real answer passes or fails depending on which machine
    ran it.
    """
    import importlib.util

    real = importlib.util.find_spec

    def _spec(name, *a, **kw):
        if name == "playwright":
            return object() if installed else None
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib.util, "find_spec", _spec)


def no_playwright(monkeypatch):
    _pretend_playwright(monkeypatch, installed=False)


def with_playwright(monkeypatch):
    _pretend_playwright(monkeypatch, installed=True)


# --- every tier reports something ---------------------------------------------


@pytest.mark.asyncio
async def test_every_tier_is_reported(monkeypatch):
    working_plain(monkeypatch)
    no_playwright(monkeypatch)

    results = await check_tier_health()

    assert [r.name for r in results] == [TIER_PLAIN, TIER_STEALTH, TIER_BROWSER]


# --- a fault is not the same as a deliberate absence --------------------------


@pytest.mark.asyncio
async def test_an_uninstalled_backend_is_off_not_broken(monkeypatch):
    """A stealth browser nobody wanted must not read as a fault.

    Collapsing the two would alert about a backend this host deliberately never
    installed — and an alert that is usually wrong gets muted, taking the real
    ones with it.
    """
    working_plain(monkeypatch)
    no_playwright(monkeypatch)

    results = _by_tier(await check_tier_health())

    assert results[TIER_STEALTH].status == TIER_OFF
    assert results[TIER_BROWSER].status == TIER_OFF
    assert "STEALTH_FETCH_COMMAND" in results[TIER_STEALTH].detail


@pytest.mark.asyncio
async def test_a_configured_backend_that_fails_is_broken(monkeypatch):
    """This is the host-rebuild case: the .env line survives, the venv does not."""
    working_plain(monkeypatch)
    no_playwright(monkeypatch)
    monkeypatch.setattr(settings, "stealth_fetch_command", "/gone/python /gone/fetch.py {url}")

    async def _fails(url):
        raise FetchBlockedError("stealth backend failed: No such file or directory")

    monkeypatch.setattr("kronos.tools.acquire.fetch_stealth", _fails)

    results = _by_tier(await check_tier_health())

    assert results[TIER_STEALTH].status == TIER_BROKEN
    assert "No such file" in results[TIER_STEALTH].detail


@pytest.mark.asyncio
async def test_a_plain_fetch_that_raises_is_broken(monkeypatch):
    no_playwright(monkeypatch)

    async def _boom(url):
        raise OSError("Network is unreachable")

    monkeypatch.setattr("kronos.tools.acquire.fetch_plain", _boom)

    results = _by_tier(await check_tier_health())

    assert results[TIER_PLAIN].status == TIER_BROKEN
    assert "Network is unreachable" in results[TIER_PLAIN].detail


@pytest.mark.asyncio
async def test_a_plain_fetch_that_gets_a_block_page_is_broken(monkeypatch):
    """example.com has no reason to refuse us; if it does, something is wrong here."""
    no_playwright(monkeypatch)

    async def _blocked(url):
        return 403, "<html><body>Access denied</body></html>"

    monkeypatch.setattr("kronos.tools.acquire.fetch_plain", _blocked)

    results = _by_tier(await check_tier_health())

    assert results[TIER_PLAIN].status == TIER_BROKEN
    assert "403" in results[TIER_PLAIN].detail


@pytest.mark.asyncio
async def test_a_working_tier_reports_how_much_it_read(monkeypatch):
    """ "ok" with no evidence is what a broken browser also said, once."""
    working_plain(monkeypatch)
    no_playwright(monkeypatch)

    results = _by_tier(await check_tier_health())

    assert results[TIER_PLAIN].status == TIER_OK
    assert results[TIER_PLAIN].ok is True
    assert "characters of text" in results[TIER_PLAIN].detail


# --- probing must not damage what it probes -----------------------------------


@pytest.mark.asyncio
async def test_the_probe_closes_a_browser_it_started(monkeypatch):
    """Otherwise a daily check leaks a headless browser onto a six-agent host."""
    working_plain(monkeypatch)
    with_playwright(monkeypatch)
    closed = []

    async def _fetch_browser(url):
        return 200, REAL_PAGE

    monkeypatch.setattr("kronos.tools.acquire.fetch_browser", _fetch_browser)
    from kronos.tools.browser import engine

    monkeypatch.setattr(engine, "is_running", lambda: False)

    async def _close():
        closed.append(True)

    monkeypatch.setattr(engine, "close", _close)

    await check_tier_health()

    assert closed == [True]


@pytest.mark.asyncio
async def test_the_probe_leaves_an_existing_session_open(monkeypatch):
    """A browser already open is most likely a signed-in site session.

    Closing it to prove an unrelated point would end that session — and the next
    task would report "needs login" for a reason nothing in its logs explains.
    """
    working_plain(monkeypatch)
    with_playwright(monkeypatch)
    closed = []

    async def _fetch_browser(url):
        return 200, REAL_PAGE

    monkeypatch.setattr("kronos.tools.acquire.fetch_browser", _fetch_browser)
    from kronos.tools.browser import engine

    monkeypatch.setattr(engine, "is_running", lambda: True)

    async def _close():
        closed.append(True)

    monkeypatch.setattr(engine, "close", _close)

    await check_tier_health()

    assert closed == []


@pytest.mark.asyncio
async def test_a_broken_browser_is_still_cleaned_up(monkeypatch):
    working_plain(monkeypatch)
    with_playwright(monkeypatch)
    closed = []

    async def _fetch_browser(url):
        raise FetchBlockedError("browser returned no usable content")

    monkeypatch.setattr("kronos.tools.acquire.fetch_browser", _fetch_browser)
    from kronos.tools.browser import engine

    monkeypatch.setattr(engine, "is_running", lambda: False)

    async def _close():
        closed.append(True)

    monkeypatch.setattr(engine, "close", _close)

    results = _by_tier(await check_tier_health())

    assert results[TIER_BROWSER].status == TIER_BROKEN
    assert closed == [True], "a failed probe still started a browser"


# --- the probe respects egress policy -----------------------------------------


@pytest.mark.asyncio
async def test_a_blocked_smoke_url_is_reported_not_bypassed(monkeypatch):
    """A health check is a strange place to put a hole in the egress policy.

    Unrunnable is also not the same as broken: reporting it as a fault would
    send somebody hunting for a browser bug that does not exist.
    """

    def _refuse(url, tool=""):
        raise RuntimeError(f"egress to {url} is not in the allowlist")

    monkeypatch.setattr("kronos.security.egress.check_url", _refuse)

    results = await check_tier_health()

    assert {r.status for r in results} == {TIER_OFF}
    assert all("cannot probe" in r.detail for r in results)


def test_the_smoke_url_is_not_a_marketplace():
    """A checker that goes red when Shopee tightens its defences gets ignored.

    The question this job asks is whether our machinery works, not whether a
    marketplace is in a good mood — those change for different reasons and only
    one of them is actionable here.
    """
    assert SMOKE_URL == "https://example.com"
