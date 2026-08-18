"""Falling back to Exa without changing what callers get back.

`brave.search` declares `list[brave.SearchResult]`, and two of its three paths
get their results from Exa instead. Exa's `SearchResult` carries identical
fields but is a different class, so handing it straight back breaks equality and
isinstance for everyone who believed the signature.

Both fallback paths are pinned here, and both pin `brave_api_key` explicitly.
The version of this test recovered from an old branch did not: it read whatever
the developer happened to have configured, passed on a machine with a Brave key,
and failed in CI — which is how the unconverted path stayed hidden.
"""

from kronos.config import settings
from kronos.tools import brave, exa

EXA_RESULT = exa.SearchResult(title="Result", url="https://example.com", description="Text")
AS_BRAVE = brave.SearchResult(title="Result", url="https://example.com", description="Text")


def _exa_returns(monkeypatch, results):
    monkeypatch.setattr(brave._exa, "search", lambda *args, **kwargs: results)


def _in_quota_cooldown(monkeypatch):
    monkeypatch.setattr(brave, "_brave_unavailable_until", brave.time.monotonic() + 60)


def test_quota_cooldown_converts_exa_results_to_brave_results(monkeypatch) -> None:
    """The path taken on a host that has a Brave key and has hit its quota."""
    monkeypatch.setattr(settings, "brave_api_key", "configured")
    _in_quota_cooldown(monkeypatch)
    _exa_returns(monkeypatch, [EXA_RESULT])

    assert brave.search("query") == [AS_BRAVE]


def test_a_host_with_no_brave_key_also_gets_brave_results(monkeypatch) -> None:
    """The path that forgot to convert, and could only be seen without a key."""
    monkeypatch.setattr(settings, "brave_api_key", "")
    _in_quota_cooldown(monkeypatch)
    _exa_returns(monkeypatch, [EXA_RESULT])

    results = brave.search("query")

    assert results == [AS_BRAVE]
    assert all(isinstance(r, brave.SearchResult) for r in results), "the declared return type is not decoration"


def test_no_key_and_no_cooldown_converts_too(monkeypatch) -> None:
    """The third way in: no key at all, so Exa is tried instead of returning empty."""
    monkeypatch.setattr(settings, "brave_api_key", "")
    monkeypatch.setattr(brave, "_brave_unavailable_until", 0.0)
    _exa_returns(monkeypatch, [EXA_RESULT])

    assert brave.search("query") == [AS_BRAVE]
