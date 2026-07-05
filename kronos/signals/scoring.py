"""Evidence scoring and trend-claim guardrails."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from kronos.signals.models import SignalItem
from kronos.signals.sources import SignalSource


class EvidenceLevel(StrEnum):
    """Allowed evidence levels for digest claims."""

    ANECDOTE = "anecdote"
    WEAK_SIGNAL = "weak_signal"
    EMERGING_SIGNAL = "emerging_signal"
    TREND = "trend"
    CONFIRMED = "confirmed"


OFFICIAL_PLATFORMS = {"competitor", "app_store", "play_store", "analytics", "seo", "rss"}
FORBIDDEN_TREND_PHRASES = (
    "рынок сдвигается",
    "все переходят",
    "главный тренд",
    "массово",
)
_PHRASE_REPLACEMENTS = {
    "рынок сдвигается": "есть единичный сигнал",
    "все переходят": "отдельные источники упоминают",
    "главный тренд": "сигнал для наблюдения",
    "массово": "в отдельных обсуждениях",
}


@dataclass(frozen=True)
class EvidenceAssessment:
    """Deterministic assessment of a cluster's evidentiary strength."""

    level: EvidenceLevel
    score: float
    item_count: int
    independent_source_count: int
    platform_count: int
    official_count: int
    expert_count: int
    max_engagement_score: float
    unique_origin_count: int
    can_make_trend_claim: bool

    @property
    def guardrail_text(self) -> str:
        if self.can_make_trend_claim:
            return "Trend language allowed when the claim is scoped to the evidence."
        forbidden = ", ".join(FORBIDDEN_TREND_PHRASES)
        return (
            f"Evidence level is {self.level}; forbid trend language: {forbidden}. "
            "Use wording like: единичный сигнал, одно обсуждение, стоит понаблюдать."
        )


def score_item(item: SignalItem, source: SignalSource | None = None) -> float:
    """Score one item before clustering."""
    score = 0.0
    trust = source.trust if source else ""
    tier = source.tier if source else ""

    if trust == "official" or item.source_platform in OFFICIAL_PLATFORMS:
        score += 35
    elif trust == "expert":
        score += 25
    elif trust == "community_high":
        score += 15
    elif trust == "community_low":
        score += 5
    elif trust == "noisy":
        score -= 20

    if tier == "core":
        score += 10
    elif tier == "quarantine":
        score -= 30

    if item.url or item.source_url:
        score += 10
    if "jb_" in ",".join(item.categories) or "travel_insights" in item.categories:
        score += 10

    score += min(engagement_score(item), 30)
    score = max(0.0, min(100.0, score))
    return score


def assess_evidence(
    items: list[SignalItem] | tuple[SignalItem, ...],
    *,
    sources_by_id: dict[str, SignalSource] | None = None,
) -> EvidenceAssessment:
    """Assess whether a cluster supports anecdote/weak/emerging/trend claims."""
    sources = sources_by_id or {}
    unique_items = _unique_by_origin(items)
    source_ids = {item.source_id for item in unique_items}
    platforms = {item.source_platform for item in unique_items}
    official_count = 0
    expert_count = 0
    score = 0.0
    max_engagement = 0.0

    for item in unique_items:
        source = sources.get(item.source_id)
        if _is_official(item, source):
            official_count += 1
        if source and source.trust == "expert":
            expert_count += 1
        item_score = item.importance_score or score_item(item, source)
        score += item_score
        max_engagement = max(max_engagement, engagement_score(item), item.importance_score)

    independent_count = len(source_ids)
    platform_count = len(platforms)
    avg_score = score / len(unique_items) if unique_items else 0.0

    level = _evidence_level(
        independent_count=independent_count,
        platform_count=platform_count,
        official_count=official_count,
        expert_count=expert_count,
        max_engagement=max_engagement,
    )
    return EvidenceAssessment(
        level=level,
        score=round(avg_score, 2),
        item_count=len(items),
        independent_source_count=independent_count,
        platform_count=platform_count,
        official_count=official_count,
        expert_count=expert_count,
        max_engagement_score=round(max_engagement, 2),
        unique_origin_count=len(unique_items),
        can_make_trend_claim=level in {EvidenceLevel.EMERGING_SIGNAL, EvidenceLevel.TREND, EvidenceLevel.CONFIRMED},
    )


def sanitize_trend_language(text: str, assessment: EvidenceAssessment) -> str:
    """Downgrade overclaiming language when evidence is below emerging_signal."""
    if assessment.can_make_trend_claim:
        return text
    sanitized = text
    for phrase, replacement in _PHRASE_REPLACEMENTS.items():
        sanitized = sanitized.replace(phrase, replacement)
    return sanitized


def _evidence_level(
    *,
    independent_count: int,
    platform_count: int,
    official_count: int,
    expert_count: int,
    max_engagement: float,
) -> EvidenceLevel:
    if official_count >= 1 and independent_count == 1:
        return EvidenceLevel.CONFIRMED
    if official_count >= 1 and independent_count >= 2 and (platform_count >= 2 or max_engagement >= 70):
        return EvidenceLevel.TREND
    if independent_count >= 3 and platform_count >= 2:
        return EvidenceLevel.TREND
    if independent_count >= 2 and (platform_count >= 2 or expert_count >= 1):
        return EvidenceLevel.EMERGING_SIGNAL
    if independent_count >= 2 or official_count >= 1 or max_engagement >= 70:
        return EvidenceLevel.WEAK_SIGNAL
    return EvidenceLevel.ANECDOTE


def _unique_by_origin(items: list[SignalItem] | tuple[SignalItem, ...]) -> list[SignalItem]:
    seen: set[str] = set()
    unique: list[SignalItem] = []
    for item in items:
        key = origin_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def origin_key(item: SignalItem) -> str:
    """Return stable origin key; same URL/origin counts once for independence."""
    url = item.url or item.source_url
    if url:
        return _normalize_url(url)
    semantic = item.normalized_text or item.text or item.title or item.source_item_key
    return f"{item.source_platform}:{item.source_id}:{' '.join(semantic.lower().split())[:160]}"


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower().removeprefix("www."),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def _is_official(item: SignalItem, source: SignalSource | None) -> bool:
    return item.source_platform in OFFICIAL_PLATFORMS or bool(source and source.trust == "official")


def engagement_score(item: SignalItem) -> float:
    """Return a 0..100 engagement proxy from views/reactions/score payload."""
    payload = item.raw_payload or {}
    views = _number(payload.get("views"))
    reactions = _number(payload.get("reactions"))
    score = _number(payload.get("score"))
    return min(100.0, reactions * 8 + views / 100 + score / 10)


def _number(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        compact = value.strip().upper().replace(",", "")
        try:
            if compact.endswith("K"):
                return float(compact[:-1]) * 1_000
            if compact.endswith("M"):
                return float(compact[:-1]) * 1_000_000
            return float(compact or 0)
        except ValueError:
            return 0.0
    return 0.0
