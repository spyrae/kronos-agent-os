from dataclasses import dataclass

import pytest

from kronos.signals.fetchers.app_stores import fetch_app_store_source, fetch_play_store_source
from kronos.signals.fetchers.base import FetcherError, FetchErrorKind, FetchOptions, FetchResult
from kronos.signals.fetchers.brave_search import fetch_search_source
from kronos.signals.fetchers.competitor import fetch_competitor_source
from kronos.signals.fetchers.jb_system import fetch_analytics_source, fetch_seo_source
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
async def test_x_fetcher_uses_official_api_when_token_is_available():
    def fake_x_api(source, options, token, *, handle):
        assert token == "token"
        assert handle == "OpenAIDevs"
        assert options.limit == 2
        return {
            "data": [
                {
                    "id": "123",
                    "text": "API update for developers",
                    "created_at": "2026-05-29T10:00:00.000Z",
                    "author_id": "42",
                    "public_metrics": {
                        "like_count": 100,
                        "retweet_count": 10,
                        "reply_count": 5,
                        "quote_count": 2,
                        "impression_count": 10000,
                    },
                    "entities": {"urls": [{"expanded_url": "https://example.com"}]},
                }
            ]
        }

    result = await fetch_x_source(
        _source("x", id="x_openai_devs", handle="@OpenAIDevs", trust="official"),
        options=FetchOptions(limit=2),
        x_api_fn=fake_x_api,
        bearer_token="token",
    )

    item = result.items[0]
    assert item.source_platform == "x"
    assert item.author == "@OpenAIDevs"
    assert item.handle == "@OpenAIDevs"
    assert item.url == "https://x.com/OpenAIDevs/status/123"
    assert item.published_at == "2026-05-29T10:00:00.000Z"
    assert item.raw_payload["backend"] == "official_x_api"
    assert item.raw_payload["public_metrics"]["like_count"] == 100
    assert item.raw_payload["raw_api_payload"]["id"] == "123"
    assert item.confidence_score == 85.0


@pytest.mark.asyncio
async def test_x_fetcher_missing_token_uses_strict_status_fallback():
    calls = []

    def fake_search(query, count, freshness):
        calls.append(query)
        return [SearchResult("OpenAI Devs", "https://x.com/OpenAIDevs/status/1", "API update")]

    def should_not_call_api(*args, **kwargs):
        raise AssertionError("X API must not be called without token")

    result = await fetch_x_source(
        _source("x", id="x_openai_devs", handle="@OpenAIDevs", trust="official"),
        options=FetchOptions(limit=3, freshness="pw"),
        search_fn=fake_search,
        x_api_fn=should_not_call_api,
        bearer_token="",
    )

    assert calls == ["site:x.com/OpenAIDevs/status Test source"]
    assert result.items[0].author == "@OpenAIDevs"
    assert result.items[0].url == "https://x.com/OpenAIDevs/status/1"
    assert result.items[0].raw_payload["backend"] == "strict_exa_status_fallback"
    assert result.items[0].confidence_score == 85.0


@pytest.mark.asyncio
async def test_x_fetcher_rejects_secondary_articles_from_fallback():
    def fake_search(query, count, freshness):
        return [
            SearchResult(
                "Article about a tweet",
                "https://example.com/news/openai-devs-tweet",
                "This article embeds an X post",
            )
        ]

    result = await fetch_x_source(
        _source("x", id="x_openai_devs", handle="@OpenAIDevs"),
        search_fn=fake_search,
        bearer_token="",
    )

    assert result.ok is True
    assert result.items == ()


@pytest.mark.asyncio
async def test_x_fetcher_accepts_matching_twitter_status_url():
    def fake_search(query, count, freshness):
        return [
            SearchResult(
                "YC status",
                "https://twitter.com/ycombinator/status/1234567890?s=20",
                "Batch update",
            )
        ]

    result = await fetch_x_source(
        _source("x", id="x_yc", handle="@ycombinator", trust="expert"),
        search_fn=fake_search,
        bearer_token="",
    )

    assert len(result.items) == 1
    assert result.items[0].url == "https://x.com/ycombinator/status/1234567890"
    assert result.items[0].source_item_key == "https://x.com/ycombinator/status/1234567890"


@pytest.mark.asyncio
async def test_x_fetcher_rejects_handle_mismatch():
    def fake_search(query, count, freshness):
        return [SearchResult("Other", "https://x.com/other/status/1", "Wrong handle")]

    result = await fetch_x_source(
        _source("x", id="x_openai_devs", handle="@OpenAIDevs"),
        search_fn=fake_search,
        bearer_token="",
    )

    assert result.items == ()


@pytest.mark.asyncio
async def test_x_fetcher_api_error_falls_back_without_failing_source():
    def fake_x_api(*args, **kwargs):
        raise ConnectionError("rate limited")

    def fake_search(query, count, freshness):
        return [SearchResult("OpenAI Devs", "https://x.com/OpenAIDevs/status/99", "Fallback")]

    result = await fetch_x_source(
        _source("x", id="x_openai_devs", handle="@OpenAIDevs"),
        search_fn=fake_search,
        x_api_fn=fake_x_api,
        bearer_token="token",
    )

    assert result.ok is True
    assert result.items[0].url == "https://x.com/OpenAIDevs/status/99"
    assert result.items[0].raw_payload["backend"] == "strict_exa_status_fallback"


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
async def test_app_store_fetcher_normalizes_metrics_and_reviews():
    result = await fetch_app_store_source(
        _source("app_store", id="jb_app_store_reviews", categories=("jb_system", "travel_insights")),
        options=FetchOptions(limit=2),
        collector=lambda: {
            "ios_rating": 4.8,
            "ios_reviews_count": 42,
            "ios_version": "1.2.3",
            "ios_release_notes": "Better trip planning",
            "ios_recent_reviews": [
                {
                    "rating": 5,
                    "title": "Great planner",
                    "body": "Made our group trip easier.",
                    "territory": "US",
                    "date": "2026-05-29",
                }
            ],
        },
    )

    assert result.ok is True
    assert len(result.items) == 2
    assert result.items[0].source_platform == "app_store"
    assert "рейтинг" in result.items[0].text.lower()
    assert result.items[1].title.startswith("Отзыв iOS 5★")
    assert result.items[1].categories == ("jb_system", "travel_insights")


@pytest.mark.asyncio
async def test_play_store_fetcher_treats_missing_optional_config_as_empty():
    result = await fetch_play_store_source(
        _source("play_store", id="jb_google_play_reviews", categories=("jb_system", "travel_insights")),
        collector=lambda: {"error": "App not found(404)."},
    )

    assert result.ok is True
    assert result.items == ()


@pytest.mark.asyncio
async def test_play_store_fetcher_normalizes_metrics():
    result = await fetch_play_store_source(
        _source("play_store", id="jb_google_play_reviews", categories=("jb_system", "travel_insights")),
        collector=lambda: {
            "android_rating": 4.6,
            "android_reviews_count": 10,
            "android_installs": 1000,
            "android_version": "1.0.0",
        },
    )

    assert result.ok is True
    assert len(result.items) == 1
    assert result.items[0].source_platform == "play_store"
    assert "рейтинг Android" in result.items[0].text


@pytest.mark.asyncio
async def test_analytics_fetcher_normalizes_product_metrics():
    result = await fetch_analytics_source(
        _source("analytics", id="jb_product_analytics", categories=("jb_system",), trust="official"),
        collector=lambda: {
            "dau": 12,
            "new_signups_24h": 3,
            "trips_created_24h": 5,
            "client_errors_24h": 0,
        },
    )

    assert result.ok is True
    assert len(result.items) == 1
    assert result.items[0].source_platform == "analytics"
    assert "создано поездок за 24ч: 5" in result.items[0].text


@pytest.mark.asyncio
async def test_analytics_fetcher_treats_missing_optional_config_as_empty():
    result = await fetch_analytics_source(
        _source("analytics", id="jb_product_analytics", categories=("jb_system",), trust="official"),
        collector=lambda: {"error": "PostHog not configured"},
    )

    assert result.ok is True
    assert result.items == ()


@pytest.mark.asyncio
async def test_seo_fetcher_normalizes_geo_snapshot():
    result = await fetch_seo_source(
        _source("seo", id="jb_seo_geo", categories=("jb_system",), trust="official"),
        collector=lambda: {
            "journeybay_top10": 4,
            "journeybay_geo_citation_rate": 12.5,
            "journeybay_gsc_clicks_28d": 123,
        },
    )

    assert result.ok is True
    assert len(result.items) == 1
    assert result.items[0].source_platform == "seo"
    assert "JourneyBay: SEO/GEO снимок" in result.items[0].title
    assert "journeybay топ-10: 4" in result.items[0].text


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
