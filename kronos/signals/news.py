"""News signal detection and prioritization helpers.

Mirrors ``ideas.py`` / ``jobs.py``: a deterministic item-level score plus an
``is_news_signal`` gate. The News sources in ``SOURCES.yaml`` are already a
curated AI/tech set, so this filter is intentionally *generous* — it drops
only clear noise (promo, giveaways, off-topic chatter) and caps volume. The
fine-grained "interesting vs noise" curation is done downstream by the LLM
editor (``digest.curate_news_digest``).
"""

from __future__ import annotations

from collections.abc import Sequence

from kronos.signals.models import SignalItem
from kronos.signals.scoring import engagement_score
from kronos.signals.sources import SignalSource

# Hard noise: promo, giveaways, crypto shilling, listicle roundups. Any hit
# here sinks the item below zero so it never reaches the digest.
NEWS_NOISE_PHRASES = (
    "giveaway",
    "airdrop",
    "coupon",
    "promo code",
    "discount code",
    "sponsored",
    "affiliate link",
    "referral code",
    "newsletter roundup",
    "top 10",
    "top ten",
    "get rich",
    "crypto signal",
    "pump and dump",
    "казино",
    "розыгрыш",
    "промокод",
    "реферальн",
    "реклама",
)
# Product launches / releases — the core of an AI-industry news feed.
NEWS_LAUNCH_PHRASES = (
    "launched",
    "launches",
    "launching",
    "released",
    "release",
    "now available",
    "generally available",
    "rolling out",
    "introducing",
    "announcing",
    "announced",
    "unveiled",
    "shipped",
    "ships",
    "open-sourced",
    "open sourced",
    "open source",
    "запустил",
    "выпустил",
    "представил",
    "анонсировал",
    "релиз",
    "доступен",
)
NEWS_RESEARCH_PHRASES = (
    "paper",
    "benchmark",
    "state of the art",
    "outperforms",
    "breakthrough",
    "исследование",
    "бенчмарк",
)
NEWS_BUSINESS_PHRASES = (
    "raised",
    "funding",
    "series a",
    "series b",
    "seed round",
    "acquired",
    "acquisition",
    "valuation",
    "partnership",
    "привлёк",
    "раунд",
    "поглощение",
)
# Model / company proper nouns that signal a concrete, trackable story.
NEWS_PRODUCT_TERMS = (
    "gpt",
    "claude",
    "gemini",
    "llama",
    "mistral",
    "deepseek",
    "qwen",
    "grok",
    "openai",
    "anthropic",
    "google",
    "meta",
    "microsoft",
    "nvidia",
    "perplexity",
    "cursor",
    "copilot",
    "llm",
    "agent",
    "model",
)
# Operational incidents worth surfacing.
NEWS_INCIDENT_PHRASES = (
    "outage",
    "is down",
    "breach",
    "vulnerability",
    "deprecat",
    "shutting down",
    "утечка",
    "уязвимость",
)


def news_signal_score(item: SignalItem) -> float:
    """Return deterministic 0..100 newsworthiness for one item."""
    text = _item_text(item)
    score = 0.0

    if any(phrase in text for phrase in NEWS_NOISE_PHRASES):
        score -= 60
    if any(phrase in text for phrase in NEWS_LAUNCH_PHRASES):
        score += 35
    if any(phrase in text for phrase in NEWS_BUSINESS_PHRASES):
        score += 30
    if any(phrase in text for phrase in NEWS_INCIDENT_PHRASES):
        score += 25
    if any(phrase in text for phrase in NEWS_RESEARCH_PHRASES):
        score += 22
    if any(term in text for term in NEWS_PRODUCT_TERMS):
        score += 10

    score += min(engagement_score(item), 30)
    if item.url or item.source_url:
        score += 8
    if len((item.text or item.normalized_text or "").strip()) >= 80:
        score += 6

    return max(0.0, min(100.0, score))


def is_news_noise(item: SignalItem) -> bool:
    """Return True for hard-noise items that must never enter the digest."""
    return any(phrase in _item_text(item) for phrase in NEWS_NOISE_PHRASES)


def is_news_signal(
    item: SignalItem,
    source: SignalSource | None = None,
    *,
    min_score: float = 10.0,
) -> bool:
    """Return True when the item is worth keeping for the News digest.

    Official/expert sources always pass (unless hard noise); everyone else
    must clear ``min_score`` so promo and near-empty chatter is dropped. The
    bar is deliberately low — recall over precision — because the LLM editor
    makes the final "interesting vs noise" call.
    """
    if is_news_noise(item):
        return False
    if source is not None and source.trust in {"official", "expert"}:
        return True
    return news_signal_score(item) >= min_score


def news_priority_score(items: Sequence[SignalItem]) -> float:
    """Cluster-level priority bonus used when ranking News clusters."""
    if not items:
        return 0.0
    best_signal = max(news_signal_score(item) for item in items)
    best_engagement = max(engagement_score(item) for item in items)
    return best_signal + best_engagement * 0.5


def _item_text(item: SignalItem) -> str:
    return f"{item.title} {item.text} {item.normalized_text}".lower()
