from kronos.signals.clustering import deduplicate_items
from kronos.signals.models import SignalItem
from kronos.signals.scoring import EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource


def _source(source_id: str, platform: str, trust: str = "community_high") -> SignalSource:
    return SignalSource(
        id=source_id,
        platform=platform,
        handle=source_id,
        categories=("news",),
        tier="core",
        trust=trust,
    )


def _item(source_id: str, platform: str, text: str, url: str = "", **kwargs) -> SignalItem:
    return SignalItem(
        source_id=source_id,
        source_platform=platform,
        title=text,
        text=text,
        url=url,
        categories=("news",),
        **kwargs,
    )


def test_single_telegram_message_is_anecdote_and_trend_language_is_downgraded():
    item = _item("telegram_nobilix_chat", "telegram", "Codex discussion in one chat")

    assessment = assess_evidence([item])
    text = "рынок сдвигается в сторону Codex, все переходят массово"

    assert assessment.level == EvidenceLevel.ANECDOTE
    assert assessment.can_make_trend_claim is False
    assert sanitize_trend_language(text, assessment) == (
        "есть единичный сигнал в сторону Codex, отдельные источники упоминают в отдельных обсуждениях"
    )
    assert "рынок сдвигается" in assessment.guardrail_text


def test_two_independent_platforms_are_emerging_signal():
    items = [
        _item("reddit_local_llama", "reddit", "Developers discuss local agents"),
        _item("x_omarsar0", "x", "Expert thread about local agents"),
    ]
    sources = {
        "reddit_local_llama": _source("reddit_local_llama", "reddit"),
        "x_omarsar0": _source("x_omarsar0", "x", trust="expert"),
    }

    assessment = assess_evidence(items, sources_by_id=sources)

    assert assessment.level == EvidenceLevel.EMERGING_SIGNAL
    assert assessment.independent_source_count == 2
    assert assessment.platform_count == 2
    assert assessment.can_make_trend_claim is True


def test_three_independent_sources_across_platforms_are_trend():
    items = [
        _item("reddit_local_llama", "reddit", "Agent tooling launch"),
        _item("x_openai_devs", "x", "Agent tooling launch from OpenAI"),
        _item("telegram_hiaimediaen", "telegram", "Agent tooling discussion"),
    ]

    assessment = assess_evidence(items)

    assert assessment.level == EvidenceLevel.TREND
    assert assessment.independent_source_count == 3
    assert assessment.platform_count == 3


def test_single_official_source_is_confirmed_but_not_market_trend():
    item = _item("x_openai_devs", "x", "Official API changelog", url="https://x.com/OpenAIDevs/status/1")
    source = _source("x_openai_devs", "x", trust="official")

    assessment = assess_evidence([item], sources_by_id={"x_openai_devs": source})

    assert assessment.level == EvidenceLevel.CONFIRMED
    assert assessment.can_make_trend_claim is True
    assert assessment.official_count == 1


def test_duplicate_urls_do_not_increase_independent_source_count():
    items = [
        _item("x_openai_devs", "x", "Official API update", url="https://x.com/OpenAIDevs/status/1?utm=foo"),
        _item("search_ai_news", "search", "Mirror of official update", url="https://www.x.com/OpenAIDevs/status/1"),
    ]

    assessment = assess_evidence(items)

    assert assessment.item_count == 2
    assert assessment.unique_origin_count == 1
    assert assessment.independent_source_count == 1
    assert assessment.level == EvidenceLevel.ANECDOTE


def test_deduplicate_items_by_url_and_text_fingerprint():
    items = [
        _item("a", "x", "Same launch text", url="https://example.com/a?ref=1"),
        _item("b", "reddit", "Different mirror", url="https://www.example.com/a"),
        _item("c", "telegram", "Same launch text"),
        _item("d", "telegram", "Fresh different signal"),
    ]

    result = deduplicate_items(items)

    assert [item.source_id for item in result.unique_items] == ["a", "d"]
    assert result.duplicate_indexes == {1: 0, 2: 0}
