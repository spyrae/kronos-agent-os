"""Evidence-aware Telegram digest rendering for Signal Intelligence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from html import escape
from typing import Any

from kronos.signals.ideas import caveat_for_items, product_angle_for_items, why_now_for_items
from kronos.signals.models import SignalDigest, SignalItem
from kronos.signals.routing import DigestRoute, route_for_category
from kronos.signals.scoring import EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource
from kronos.signals.store import SignalStore
from kronos.signals.travel import journeybay_implication_for_items, travel_caveat_for_items

TELEGRAM_SAFE_MAX_CHARS = 3900
MAX_IDEA_CLUSTERS = 10
MAX_TRAVEL_CLUSTERS = 10
TITLE_BY_CATEGORY = {
    "news": "Новости и AI-индустрия",
    "jobs": "Вакансии и сигналы найма",
    "ideas": "Продуктовые и бизнес-идеи",
    "travel_insights": "JourneyBay: инсайты о путешествиях",
    "jb_competitors": "JourneyBay: конкуренты",
    "jb_system": "JourneyBay: статус системы",
}
SECTION_TITLES = {
    "confirmed": "✅ Подтверждено / официально",
    "emerging": "📈 Формирующиеся сигналы",
    "watchlist": "👀 Наблюдения к проверке",
}
EVIDENCE_LABELS = {
    EvidenceLevel.ANECDOTE: "единичное наблюдение",
    EvidenceLevel.WEAK_SIGNAL: "слабый сигнал",
    EvidenceLevel.EMERGING_SIGNAL: "формирующийся сигнал",
    EvidenceLevel.TREND: "подтверждаемый тренд",
    EvidenceLevel.CONFIRMED: "подтверждено",
}


@dataclass(frozen=True)
class RenderedDigest:
    """Telegram-ready digest artifact."""

    route: DigestRoute
    title: str
    body: str
    categories: tuple[str, ...]
    cluster_ids: tuple[int, ...]
    item_ids: tuple[int, ...]


def render_digest(
    category: str,
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    *,
    sources_by_id: Mapping[str, SignalSource] | None = None,
    max_chars: int = TELEGRAM_SAFE_MAX_CHARS,
) -> RenderedDigest:
    """Render scored clusters into a Telegram HTML digest."""
    route = route_for_category(category)
    source_map = dict(sources_by_id or {})
    selected_clusters = [cluster for cluster in clusters if _cluster_category(cluster) == route.category]
    selected_clusters = _rank_clusters(selected_clusters, items_by_cluster, source_map, category=route.category)
    if route.category == "ideas":
        selected_clusters = selected_clusters[:MAX_IDEA_CLUSTERS]
    if route.category == "travel_insights":
        selected_clusters = selected_clusters[:MAX_TRAVEL_CLUSTERS]
    title = f"{TITLE_BY_CATEGORY.get(route.category, route.destination)} — обзор сигналов"

    lines = [f"<b>{escape(title)}</b>", ""]
    if not selected_clusters:
        lines.append("<i>За это окно нет достаточно сильных сигналов.</i>")
        return RenderedDigest(route, title, "\n".join(lines), (route.category,), (), ())

    sections = _group_clusters(selected_clusters, items_by_cluster, source_map, category=route.category)
    for section_title, rows in sections:
        if not rows:
            continue
        lines.append(f"<b>{escape(section_title)}</b>")
        lines.extend(rows)
        lines.append("")

    cluster_ids = tuple(int(cluster.get("id", 0) or 0) for cluster in selected_clusters if cluster.get("id"))
    item_ids = tuple(
        int(item_id)
        for cluster in selected_clusters
        for item_id in (cluster.get("item_ids") or [])
        if item_id
    )
    body = _truncate_html("\n".join(lines).strip(), max_chars=max_chars)
    return RenderedDigest(route, title, body, (route.category,), cluster_ids, item_ids)


def save_rendered_digest(store: SignalStore, digest: RenderedDigest, *, dry_run: bool = True) -> int:
    """Persist rendered digest metadata; dry-run artifacts are marked in title."""
    title = f"[dry-run] {digest.title}" if dry_run else digest.title
    return store.save_digest(
        SignalDigest(
            destination=digest.route.destination,
            title=title,
            body=digest.body,
            categories=digest.categories,
            item_ids=digest.item_ids,
            cluster_ids=digest.cluster_ids,
        ),
        count_in_quality=not dry_run,
    )


def polish_rendered_digest(digest: RenderedDigest, *, max_chars: int = TELEGRAM_SAFE_MAX_CHARS) -> RenderedDigest:
    """Translate/clean a rendered digest for Russian Telegram presentation."""
    body = _clean_digest_markup(digest.body)
    polished = _polish_digest_with_llm(digest.route.category, body, max_chars=max_chars)
    return replace(digest, body=_truncate_html(_clean_digest_markup(polished or body), max_chars=max_chars))


def _group_clusters(
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    sources_by_id: dict[str, SignalSource],
    *,
    category: str,
) -> list[tuple[str, list[str]]]:
    sections = {
        SECTION_TITLES["confirmed"]: [],
        SECTION_TITLES["emerging"]: [],
        SECTION_TITLES["watchlist"]: [],
    }
    for cluster in clusters:
        cluster_id = int(cluster.get("id", 0) or 0)
        items = tuple(items_by_cluster.get(cluster_id, ()))
        assessment = assess_evidence(items, sources_by_id=sources_by_id)
        rendered = _render_cluster(cluster, items, assessment, category=category)
        if assessment.level == EvidenceLevel.CONFIRMED:
            sections[SECTION_TITLES["confirmed"]].append(rendered)
        elif assessment.level in {EvidenceLevel.EMERGING_SIGNAL, EvidenceLevel.TREND}:
            sections[SECTION_TITLES["emerging"]].append(rendered)
        else:
            sections[SECTION_TITLES["watchlist"]].append(rendered)

    return [(title, rows) for title, rows in sections.items()]


def _rank_clusters(
    clusters: Sequence[Mapping[str, Any]],
    items_by_cluster: Mapping[int, Sequence[SignalItem]],
    sources_by_id: dict[str, SignalSource],
    *,
    category: str,
) -> list[Mapping[str, Any]]:
    def sort_key(cluster: Mapping[str, Any]) -> tuple[float, ...]:
        cluster_id = int(cluster.get("id", 0) or 0)
        items = tuple(items_by_cluster.get(cluster_id, ()))
        assessment = assess_evidence(items, sources_by_id=sources_by_id)
        level_rank = {
            EvidenceLevel.CONFIRMED: 5,
            EvidenceLevel.TREND: 4,
            EvidenceLevel.EMERGING_SIGNAL: 3,
            EvidenceLevel.WEAK_SIGNAL: 2,
            EvidenceLevel.ANECDOTE: 1,
        }[assessment.level]
        cluster_score = _float(cluster.get("importance_score")) or _float(cluster.get("confidence_score"))
        category_bonus = 0.0
        if category == "ideas":
            category_bonus = _idea_applicability_score(items)
        elif category == "travel_insights":
            category_bonus = _travel_applicability_score(items)
        return (
            float(level_rank),
            float(assessment.independent_source_count),
            float(assessment.platform_count),
            float(assessment.score),
            category_bonus,
            cluster_score,
        )

    return sorted(clusters, key=sort_key, reverse=True)


def _render_cluster(
    cluster: Mapping[str, Any],
    items: Sequence[SignalItem],
    assessment,
    *,
    category: str,
) -> str:
    if category == "ideas":
        return _render_idea_cluster(cluster, items, assessment)
    if category == "travel_insights":
        return _render_travel_cluster(cluster, items, assessment)

    title = _clean_display_text(sanitize_trend_language(str(cluster.get("title") or "Без названия"), assessment))
    summary = _clean_display_text(sanitize_trend_language(str(cluster.get("summary") or ""), assessment))
    evidence = _evidence_text(assessment)
    first_url = next((item.url for item in items if item.url), "")
    link = f' (<a href="{escape(first_url, quote=True)}">источник</a>)' if first_url else ""

    parts = [
        f"• <b>{escape(title)}</b>{link}",
        f"  <i>Доказательность: {escape(evidence)}</i>",
    ]
    if summary:
        parts.append(f"  {escape(summary)}")
    if not assessment.can_make_trend_claim:
        parts.append("  <i>Осторожно: это наблюдение, а не доказанный тренд.</i>")
    return "\n".join(parts)


def _render_idea_cluster(cluster: Mapping[str, Any], items: Sequence[SignalItem], assessment) -> str:
    title = _clean_display_text(sanitize_trend_language(str(cluster.get("title") or "Идея без названия"), assessment))
    summary = _clean_display_text(sanitize_trend_language(str(cluster.get("summary") or ""), assessment))
    evidence = _evidence_text(assessment)
    first_url = next((item.url for item in items if item.url), "")
    link = f' (<a href="{escape(first_url, quote=True)}">источник</a>)' if first_url else ""
    caveat = caveat_for_items(items, can_make_trend_claim=assessment.can_make_trend_claim)
    why_now = why_now_for_items(items, can_make_trend_claim=assessment.can_make_trend_claim)

    parts = [
        f"• <b>Идея:</b> {escape(title)}{link}",
        f"  <i>Доказательность: {escape(evidence)}</i>",
    ]
    if summary:
        parts.append(f"  <b>Боль / возможность:</b> {escape(summary)}")
    parts.extend(
        [
            f"  <b>Продуктовый угол:</b> {escape(product_angle_for_items(items))}",
            f"  <b>Почему сейчас:</b> {escape(why_now)}",
            f"  <b>Ограничение:</b> {escape(caveat)}",
        ]
    )
    if not assessment.can_make_trend_claim:
        parts.append("  <i>Осторожно: это вход для discovery, а не подтверждённый спрос.</i>")
    return "\n".join(parts)


def _render_travel_cluster(cluster: Mapping[str, Any], items: Sequence[SignalItem], assessment) -> str:
    title = _clean_display_text(
        sanitize_trend_language(str(cluster.get("title") or "Инсайт о путешествиях"), assessment)
    )
    summary = _clean_display_text(sanitize_trend_language(str(cluster.get("summary") or ""), assessment))
    evidence = _evidence_text(assessment)
    first_url = next((item.url for item in items if item.url), "")
    link = f' (<a href="{escape(first_url, quote=True)}">источник</a>)' if first_url else ""
    caveat = travel_caveat_for_items(items, can_make_trend_claim=assessment.can_make_trend_claim)

    parts = [
        f"• <b>Инсайт:</b> {escape(title)}{link}",
        f"  <i>Доказательность: {escape(evidence)}</i>",
    ]
    if summary:
        parts.append(f"  <b>Проблема / боль:</b> {escape(summary)}")
    parts.extend(
        [
            f"  <b>Что это значит для JourneyBay:</b> {escape(journeybay_implication_for_items(items))}",
            f"  <b>Ограничение:</b> {escape(caveat)}",
        ]
    )
    if not assessment.can_make_trend_claim:
        parts.append("  <i>Осторожно: пока нельзя называть это трендом рынка путешествий.</i>")
    return "\n".join(parts)


def _cluster_category(cluster: Mapping[str, Any]) -> str:
    return str(cluster.get("category") or "").strip().lower()


def _idea_applicability_score(items: Sequence[SignalItem]) -> float:
    text = " ".join(f"{item.title} {item.text} {item.normalized_text}".lower() for item in items)
    score = 0.0
    for phrase in ("i wish", "looking for a tool", "pain point", "problem", "автоматизировать", "боль"):
        if phrase in text:
            score += 10
    for phrase in ("travel", "itinerary", "developer", "coding", "workflow", "automation"):
        if phrase in text:
            score += 5
    return score


def _travel_applicability_score(items: Sequence[SignalItem]) -> float:
    text = " ".join(f"{item.title} {item.text} {item.normalized_text}".lower() for item in items)
    score = 0.0
    for phrase in ("itinerary", "trip planner", "booking", "reservation", "maps", "offline", "visa", "budget"):
        if phrase in text:
            score += 8
    for phrase in ("problem", "pain", "wish", "hard to", "can't share", "manual", "confusing"):
        if phrase in text:
            score += 10
    return score


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _evidence_text(assessment) -> str:
    source_word = _plural_ru(assessment.independent_source_count, "источник", "источника", "источников")
    platform_word = _plural_ru(assessment.platform_count, "платформа", "платформы", "платформ")
    level = EVIDENCE_LABELS.get(assessment.level, str(assessment.level))
    return (
        f"{assessment.independent_source_count} {source_word} / "
        f"{assessment.platform_count} {platform_word} · {level}"
    )


def _plural_ru(number: int, one: str, few: str, many: str) -> str:
    number = abs(number) % 100
    if 11 <= number <= 19:
        return many
    last = number % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _clean_display_text(text: str, *, limit: int = 420) -> str:
    cleaned = _clean_markdown_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _clean_markdown_text(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 — \2", cleaned)
    cleaned = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", cleaned)
    cleaned = cleaned.replace("```", "")
    cleaned = re.sub(r"(^|\s)#{1,6}\s+", r"\1", cleaned)
    cleaned = re.sub(r"(\*\*|__)(.*?)\1", r"\2", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\*)([^*]+)(?<!\*)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = cleaned.replace("\\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -*_•\n\t")


def _clean_digest_markup(body: str) -> str:
    cleaned = body.strip()
    cleaned = re.sub(r"```(?:html)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "")
    cleaned = re.sub(r"(?<!\*)\*\*(?!\*)", "", cleaned)
    cleaned = cleaned.replace("__", "")
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 — \2", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _polish_digest_with_llm(category: str, body: str, *, max_chars: int) -> str:
    if not _needs_russian_polish(body):
        return body

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from kronos.llm import ModelTier, invoke_with_fallback, is_runtime_llm_configured

        if not is_runtime_llm_configured():
            return body

        prompt = (
            "Отредактируй Telegram HTML-дайджест для русского пользователя.\n"
            "Задачи:\n"
            "1. Переведи ВЕСЬ английский текст на русский.\n"
            "2. Убери markdown-мусор: **, ###, backticks, markdown-ссылки.\n"
            "3. Сохрани факты, числа и смысл; ничего не добавляй.\n"
            "4. Сохрани все <a href=\"...\"> ссылки и URL.\n"
            "5. Используй только Telegram HTML-теги: <b>, <i>, <a>.\n"
            "6. Стиль: коротко, чисто, красиво, без канцелярита.\n"
            f"7. Итог максимум {max_chars - 120} символов.\n"
            "Верни только готовый HTML без пояснений.\n\n"
            f"Категория: {category}\n\n"
            f"{body}"
        )
        response = invoke_with_fallback(
            [
                SystemMessage(content="Ты редактор русскоязычных Telegram-дайджестов."),
                HumanMessage(content=prompt),
            ],
            tier=ModelTier.LITE,
        )
        content = response.content if isinstance(response.content, str) else str(response.content)
        return _clean_digest_markup(content) if content and len(content) > 20 else body
    except Exception:
        return body


def _needs_russian_polish(text: str) -> bool:
    latin_words = re.findall(r"\b[A-Za-z][A-Za-z]{3,}\b", text)
    markdown_noise = any(marker in text for marker in ("**", "```", "]("))
    return markdown_noise or len(latin_words) >= 3


def _truncate_html(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n\n<i>…обрезано под лимит Telegram; полный артефакт сохранён.</i>"
    budget = max_chars - len(suffix) - 2
    if budget <= 0:
        return suffix[-max_chars:]

    lines: list[str] = []
    length = 0
    for line in text.splitlines():
        next_length = length + len(line) + (1 if lines else 0)
        if next_length > budget:
            break
        lines.append(line)
        length = next_length

    body = "\n".join(lines).rstrip()
    if not body:
        return suffix.strip()[:max_chars]
    return f"{body}{suffix}"
