from kronos.tools import brave, exa


def test_quota_cooldown_converts_exa_results_to_brave_results(monkeypatch) -> None:
    monkeypatch.setattr(brave, "_brave_unavailable_until", brave.time.monotonic() + 60)
    monkeypatch.setattr(
        brave._exa,
        "search",
        lambda *args, **kwargs: [exa.SearchResult(title="Result", url="https://example.com", description="Text")],
    )

    results = brave.search("query")

    assert results == [brave.SearchResult(title="Result", url="https://example.com", description="Text")]
