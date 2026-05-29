from dataclasses import dataclass

import pytest

from kronos.signals.fetchers.base import FetcherError, FetchErrorKind, FetchOptions, FetchResult
from kronos.signals.fetchers.brave_search import fetch_search_source
from kronos.signals.fetchers.competitor import fetch_competitor_source
from kronos.signals.fetchers.reddit_search import fetch_reddit_source
from kronos.signals.fetchers.runner import fetch_sources, format_dry_run
from kronos.signals.fetchers.telegram_public import fetch_telegram_public_source
from kronos.signals.fetchers.telegram_telethon import fetch_telegram_telethon_source
from kronos.signals.fetchers.x_search import fetch_x_source
from kronos.signals.sources import SignalSource
from kronos.tools.brave import SearchResult


def _source(platform: str, **kwargs) -> SignalSource:
    return SignalSource(
        id=kwargs.pop("id", f"{platform}_source"),
        platform=platform,
        handle=kwargs.pop("handle", ""),
        url=kwargs.pop("url", ""),
        query=kwargs.pop("query", ""),
        categories=kwargs.pop("categories", ("news",)),
        tier=kwargs.pop("tier", "core"),
        trust=kwargs.pop("trust", "community_high"),
        description=kwargs.pop("description", "Test source"),
        filters=kwargs.pop("filters", {}),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_search_fetcher_normalizes_results():
    calls = []

    def fake_search(query, count, freshness):
        calls.append((query, count, freshness))
        return [SearchResult("Launch", "https://example.com/launch", "New AI launch")]

    result = await fetch_search_source(
        _source("search", id="search_ai", query="AI launch"),
        options=FetchOptions(limit=3, freshness="pw"),
        search_fn=fake_search,
    )

    assert calls == [("AI launch", 3, "pw")]
    assert result.ok is True
    assert result.items[0].title == "Launch"
    assert result.items[0].source_id == "search_ai"
    assert result.items[0].raw_payload["query"] == "AI launch"


@pytest.mark.asyncio
async def test_reddit_fetcher_builds_site_query():
    calls = []

    def fake_search(query, count, freshness):
        calls.append(query)
        return [SearchResult("Reddit post", "https://reddit.com/r/LocalLLaMA/1", "Post body")]

    result = await fetch_reddit_source(
        _source(
            "reddit",
            id="reddit_local_llama",
            handle="r/LocalLLaMA",
            description="Local model releases",
        ),
        search_fn=fake_search,
    )

    assert calls == ["site:reddit.com r/LocalLLaMA Local model releases"]
    assert result.items[0].source_platform == "reddit"
    assert result.items[0].url == "https://reddit.com/r/LocalLLaMA/1"


@pytest.mark.asyncio
async def test_x_fetcher_builds_site_query_and_author():
    calls = []

    def fake_search(query, count, freshness):
        calls.append(query)
        return [SearchResult("OpenAI Devs", "https://x.com/OpenAIDevs/status/1", "API update")]

    result = await fetch_x_source(
        _source("x", id="x_openai_devs", handle="@OpenAIDevs", trust="official"),
        search_fn=fake_search,
    )

    assert calls == ["site:x.com/OpenAIDevs Test source"]
    assert result.items[0].author == "@OpenAIDevs"
    assert result.items[0].confidence_score == 85.0


@dataclass
class FakeTgPost:
    id: int
    date: str
    views: str
    reactions: int
    fwd_from: str
    fwd_link: str
    text: str
    media_url: str

    @property
    def views_numeric(self) -> int:
        return 1200


@pytest.mark.asyncio
async def test_telegram_public_fetcher_normalizes_posts():
    async def fake_fetch_posts(channel, limit):
        assert channel == "@hiaimediaen"
        assert limit == 2
        return [
            FakeTgPost(
                id=42,
                date="2026-05-29T00:00:00+00:00",
                views="1.2K",
                reactions=5,
                fwd_from="",
                fwd_link="",
                text="Important AI media update",
                media_url="",
            )
        ]

    result = await fetch_telegram_public_source(
        _source("telegram", id="telegram_hiaimediaen", handle="@hiaimediaen"),
        options=FetchOptions(limit=2),
        fetch_posts=fake_fetch_posts,
    )

    assert result.items[0].url == "https://t.me/hiaimediaen/42"
    assert result.items[0].importance_score == 52.0
    assert result.items[0].raw_payload["views"] == "1.2K"


@pytest.mark.asyncio
async def test_telegram_telethon_fetcher_normalizes_messages():
    async def fake_message_fetcher(source_id, hours):
        assert source_id == "@ai_chat_cutcode"
        assert hours == 12
        return [
            {
                "text": "Hiring AI agent engineer",
                "author": "Alice",
                "reactions": 4,
                "views": 300,
                "date": "12:00",
                "urls": ["https://jobs.example.com"],
                "post_link": "https://t.me/ai_chat_cutcode/7",
            }
        ]

    result = await fetch_telegram_telethon_source(
        _source(
            "telegram",
            id="telegram_ai_chat_cutcode",
            handle="@ai_chat_cutcode",
            categories=("jobs",),
            filters={"lookback_hours": 12},
        ),
        message_fetcher=fake_message_fetcher,
    )

    assert result.items[0].source_item_key == "https://t.me/ai_chat_cutcode/7"
    assert result.items[0].author == "Alice"
    assert result.items[0].categories == ("jobs",)


@pytest.mark.asyncio
async def test_telegram_telethon_fetcher_applies_source_engagement_filters():
    async def fake_message_fetcher(source_id, hours):
        return [
            {
                "text": "Low-signal chat question that should not enter the signal digest",
                "author": "Bob",
                "reactions": 0,
                "views": 10,
                "date": "12:00",
                "urls": [],
                "post_link": "https://t.me/ai_chat_cutcode/1",
            },
            {
                "text": "High-signal AI launch discussion with enough engagement",
                "author": "Alice",
                "reactions": 5,
                "views": 100,
                "date": "12:05",
                "urls": [],
                "post_link": "https://t.me/ai_chat_cutcode/2",
            },
        ]

    result = await fetch_telegram_telethon_source(
        _source(
            "telegram",
            id="telegram_ai_chat_cutcode",
            handle="@ai_chat_cutcode",
            filters={"min_reactions": 3, "min_views": 200},
        ),
        message_fetcher=fake_message_fetcher,
    )

    assert [item.url for item in result.items] == ["https://t.me/ai_chat_cutcode/2"]


@pytest.mark.asyncio
async def test_competitor_fetcher_normalizes_changes():
    def fake_change_loader(source):
        assert source.handle == "wanderlog"
        return [
            {
                "id": 10,
                "competitor_id": "wanderlog",
                "channel": "ios",
                "change_type": "rating_drop",
                "severity": "important",
                "summary": "Rating dropped after release",
                "detected_at": "2026-05-29T00:00:00Z",
            }
        ]

    result = await fetch_competitor_source(
        _source("competitor", id="competitor_wanderlog", handle="wanderlog"),
        change_loader=fake_change_loader,
    )

    assert result.items[0].title == "wanderlog: rating_drop"
    assert result.items[0].importance_score == 70.0
    assert result.items[0].raw_payload["channel"] == "ios"


@pytest.mark.asyncio
async def test_runner_continues_after_failed_source():
    good = _source("search", id="good", query="AI")
    bad = _source("x", id="bad", handle="@bad")

    async def good_fetcher(source, options):
        return FetchResult(source=source)

    async def bad_fetcher(source, options):
        raise FetcherError(FetchErrorKind.SOURCE_UNAVAILABLE, "timeout")

    results = await fetch_sources(
        (good, bad),
        fetchers={
            "search": good_fetcher,
            "x": bad_fetcher,
        },
    )

    assert len(results) == 2
    assert results[0].ok is True
    assert results[1].errors[0].kind == FetchErrorKind.SOURCE_UNAVAILABLE
    assert "bad [x]: ERROR" in format_dry_run(results)
