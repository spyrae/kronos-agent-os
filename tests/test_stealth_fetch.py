"""The adapter between the stealth backend and the fetch tiers.

This exists because of a specific bug. The wrapper here used to be a
general-purpose scraper that, when an optional parser was missing, printed
"Install scrapling for CSS extraction" to stdout and exited 0 — and 37
characters of advice is indistinguishable from a short page to anything
downstream. `acquire.py` now validates what a backend returns, but a backend
that lies is still the wrong shape. What is pinned here is that this script
can only ever hand back a page or a non-zero exit.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stealth_fetch.py"


@pytest.fixture
def fetcher():
    spec = importlib.util.spec_from_file_location("stealth_fetch_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBrowser:
    def __init__(self, html="", raises=None):
        self.html = html
        self.raises = raises
        self.closed = False

    def new_page(self):
        return self

    def goto(self, url, timeout=None, wait_until=None):
        if self.raises:
            raise self.raises

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.html

    def close(self):
        self.closed = True


@pytest.fixture
def backend(monkeypatch):
    """Install a fake `cloakbrowser` for the script to import."""
    import types

    def _install(browser):
        module = types.ModuleType("cloakbrowser")
        module.launch = lambda **kw: browser
        monkeypatch.setitem(sys.modules, "cloakbrowser", module)
        return browser

    return _install


# --- the page, and nothing else -----------------------------------------------


def test_a_fetched_page_goes_to_stdout(fetcher, backend, capsys):
    backend(FakeBrowser(html="<html><body>Example Domain</body></html>"))

    code = fetcher.main(["https://example.com"])

    assert code == 0
    assert capsys.readouterr().out == "<html><body>Example Domain</body></html>"


def test_a_missing_backend_exits_nonzero_and_prints_no_page(fetcher, monkeypatch, capsys):
    """The expected answer on a host that never installed it — not a traceback."""
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)

    code = fetcher.main(["https://example.com"])

    captured = capsys.readouterr()
    assert code == fetcher.EXIT_BACKEND_MISSING
    assert captured.out == "", "advice on stdout is what the caller reads as a page"
    assert "setup-stealth.sh" in captured.err


def test_an_empty_document_is_a_failure_not_an_empty_page(fetcher, backend, capsys):
    """Exit 0 with nothing is how "the site has no products" gets invented."""
    backend(FakeBrowser(html="   \n  "))

    code = fetcher.main(["https://example.com"])

    captured = capsys.readouterr()
    assert code == fetcher.EXIT_NO_PAGE
    assert captured.out == ""
    assert "empty document" in captured.err


def test_a_navigation_failure_is_reported_on_stderr(fetcher, backend, capsys):
    backend(FakeBrowser(raises=TimeoutError("navigation timeout")))

    code = fetcher.main(["https://example.com"])

    captured = capsys.readouterr()
    assert code == fetcher.EXIT_FETCH_FAILED
    assert captured.out == ""
    assert "navigation timeout" in captured.err


# --- housekeeping --------------------------------------------------------------


def test_the_browser_is_closed_even_when_the_fetch_fails(fetcher, backend):
    """A leaked headless browser on a six-agent host is nobody's obvious problem."""
    browser = backend(FakeBrowser(raises=RuntimeError("boom")))

    fetcher.main(["https://example.com"])

    assert browser.closed is True


def test_nothing_but_the_page_is_ever_written_to_stdout():
    """Read from the source, so a future edit cannot quietly reintroduce the bug.

    Every diagnostic must name stderr explicitly; the single unqualified write
    is the page itself.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    prints_to_stdout = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and not any(kw.arg == "file" for kw in node.keywords)
    ]

    assert prints_to_stdout == [], "a diagnostic on stdout is read downstream as page content"
