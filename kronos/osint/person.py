"""Explicit OSINT person dossier builder.

This module is intentionally command-driven: it never runs as a background
collector and saves only curated dossier markdown, not raw search dumps.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kronos.security.pii import mask_pii
from kronos.security.sanitize import sanitize_text
from kronos.workspace import Workspace, ws

MAX_SOURCES = 5
MAX_FACT_CHARS = 280
MAX_SUMMARY_CHARS = 500
CONFIDENCE_VALUES = {"low", "medium", "high"}

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")

_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


@dataclass(frozen=True)
class SourceLink:
    """One public source used in a person dossier."""

    title: str
    url: str
    description: str = ""


@dataclass(frozen=True)
class DossierFact:
    """Source-backed claim candidate for a person dossier."""

    text: str
    source: str = ""
    confidence: str = "low"


@dataclass(frozen=True)
class DossierInference:
    """Explicit inference or unsourced claim with low confidence."""

    text: str
    confidence: str = "low"
    reason: str = ""


@dataclass(frozen=True)
class DossierResult:
    """Saved person dossier artifact."""

    query: str
    slug: str
    path: Path
    markdown: str
    source_count: int
    fact_count: int
    inference_count: int
    last_verified_at: str
    warnings: tuple[str, ...] = ()


SearchProvider = Callable[..., Sequence[Any]]


def slugify_person_name(name: str) -> str:
    """Return a stable filename slug for a person query."""
    cleaned = _clean_inline(name, limit=120).casefold().translate(_TRANSLIT)
    normalized = unicodedata.normalize("NFKD", cleaned).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or "person"


def build_person_dossier(
    query: str,
    *,
    workspace: Workspace | None = None,
    searcher: SearchProvider | None = None,
    facts: Sequence[DossierFact | Mapping[str, Any]] | None = None,
    source_links: Sequence[SourceLink | Mapping[str, Any]] | None = None,
    inferences: Sequence[DossierInference | Mapping[str, Any] | str] | None = None,
    open_questions: Sequence[str] | None = None,
    now: datetime | None = None,
    max_sources: int = MAX_SOURCES,
) -> DossierResult:
    """Build and save a curated public-source person dossier.

    The default wrapper uses existing KAOS search tooling and keeps only short
    source-backed claims and links. Tests should pass ``searcher`` or explicit
    facts to avoid network access.
    """
    person_name = _normalize_query(query)
    _validate_public_person_query(person_name)
    current_time = now or datetime.now(UTC)
    last_verified = _timestamp(current_time)
    active_workspace = workspace or ws

    links = [link for link in (_coerce_source_link(item) for item in (source_links or [])) if link.url]
    fact_items = [_coerce_fact(item) for item in (facts or [])]

    if not links and not fact_items:
        links = _search_public_sources(person_name, searcher=searcher, max_sources=max_sources)
        fact_items = _facts_from_sources(links)

    source_facts, unsourced_inferences = _split_source_backed_facts(fact_items)
    inference_items = [_coerce_inference(item) for item in (inferences or [])]
    inference_items.extend(unsourced_inferences)
    questions = tuple(_clean_inline(item, limit=180) for item in (open_questions or ()))
    if not questions:
        questions = _default_open_questions(person_name, has_sources=bool(links))

    markdown = _render_dossier(
        person_name,
        source_facts=source_facts,
        inferences=tuple(inference_items),
        source_links=tuple(links),
        open_questions=questions,
        last_verified_at=last_verified,
    )
    contacts_dir = active_workspace.contacts_dir
    contacts_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify_person_name(person_name)
    path = contacts_dir / f"{slug}.md"
    path.write_text(markdown, encoding="utf-8")

    warnings: list[str] = []
    if not links:
        warnings.append("no_public_sources")
    if not source_facts:
        warnings.append("no_source_backed_facts")

    return DossierResult(
        query=person_name,
        slug=slug,
        path=path,
        markdown=markdown,
        source_count=len(links),
        fact_count=len(source_facts),
        inference_count=len(inference_items),
        last_verified_at=last_verified,
        warnings=tuple(warnings),
    )


def handle_osint_command(
    text: str,
    *,
    workspace: Workspace | None = None,
    searcher: SearchProvider | None = None,
    now: datetime | None = None,
) -> str | None:
    """Handle `/osint person ...` and return a Telegram-safe response."""
    stripped = (text or "").strip()
    if not stripped.casefold().startswith("/osint"):
        return None

    parts = stripped.split(maxsplit=2)
    if len(parts) < 2 or parts[1].casefold() in {"help", "-h", "--help"}:
        return osint_help()
    if parts[1].casefold() != "person":
        return f"Unknown OSINT command: {parts[1]}\n\n{osint_help()}"
    if len(parts) < 3 or not parts[2].strip():
        return f"OSINT command error: person query is required.\n\n{osint_help()}"

    try:
        active_workspace = workspace or ws
        result = build_person_dossier(
            parts[2],
            workspace=active_workspace,
            searcher=searcher,
            now=now,
        )
    except ValueError as exc:
        return f"OSINT command error: {exc}\n\n{osint_help()}"

    rel_path = _relative_path(result.path, active_workspace.root)
    suffix = ""
    if result.warnings:
        suffix = f"\nWarnings: {', '.join(result.warnings)}"
    return f"Собрал dossier: {rel_path}{suffix}"


def osint_help() -> str:
    """Return supported explicit OSINT commands."""
    return "OSINT commands:\n/osint person <public name or handle> — build person dossier"


def _search_public_sources(
    person_name: str,
    *,
    searcher: SearchProvider | None,
    max_sources: int,
) -> list[SourceLink]:
    provider = searcher or _default_searcher()
    query = f'"{person_name}" person profile role company public links'
    try:
        raw_results = provider(query, count=max_sources, freshness="")
    except TypeError:
        raw_results = provider(query)
    links = [_coerce_source_link(item) for item in list(raw_results)[:max_sources]]
    return [link for link in links if link.url]


def _facts_from_sources(links: Sequence[SourceLink]) -> list[DossierFact]:
    facts: list[DossierFact] = []
    for link in links:
        text = _clean_inline(" — ".join(part for part in (link.title, link.description) if part), MAX_FACT_CHARS)
        if not text:
            continue
        facts.append(DossierFact(text=text, source=link.url, confidence="low"))
    return facts


def _split_source_backed_facts(
    facts: Sequence[DossierFact],
) -> tuple[tuple[DossierFact, ...], list[DossierInference]]:
    source_facts: list[DossierFact] = []
    inferences: list[DossierInference] = []
    for fact in facts:
        text = _clean_inline(fact.text, MAX_FACT_CHARS)
        if not text:
            continue
        confidence = _normalize_confidence(fact.confidence)
        source = _clean_url(fact.source)
        if source:
            source_facts.append(
                DossierFact(
                    text=text,
                    source=source,
                    confidence=confidence,
                )
            )
        else:
            inferences.append(
                DossierInference(
                    text=text,
                    confidence="low",
                    reason="No public source supplied; treat as inference, not fact.",
                )
            )
    return tuple(source_facts), inferences


def _render_dossier(
    person_name: str,
    *,
    source_facts: tuple[DossierFact, ...],
    inferences: tuple[DossierInference, ...],
    source_links: tuple[SourceLink, ...],
    open_questions: tuple[str, ...],
    last_verified_at: str,
) -> str:
    summary = _summary(person_name, source_facts=source_facts, source_links=source_links)
    lines = [
        f"# Person: {person_name}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Known facts",
        "",
    ]
    if source_facts:
        for fact in source_facts:
            lines.extend(
                [
                    f"- {fact.text}",
                    f"  - source: {fact.source}",
                    f"  - confidence: {fact.confidence}",
                ]
            )
    else:
        lines.append("- No source-backed facts collected yet.")

    lines.extend(["", "## Inferences", ""])
    if inferences:
        for inference in inferences:
            lines.extend(
                [
                    f"- {inference.text}",
                    f"  - confidence: {_normalize_confidence(inference.confidence)}",
                    f"  - reason: {inference.reason or 'No source-backed evidence.'}",
                ]
            )
    else:
        lines.append("- No inferences recorded.")

    lines.extend(
        [
            "",
            "## Current roles",
            "",
            _unknown_or_fact_hint(source_facts),
            "",
            "## Companies / projects",
            "",
            _unknown_or_fact_hint(source_facts),
            "",
            "## Public links",
            "",
        ]
    )
    if source_links:
        for link in source_links:
            label = link.title or _hostname(link.url) or link.url
            lines.append(f"- [{label}]({link.url})")
    else:
        lines.append("- No public links collected.")

    lines.extend(
        [
            "",
            "## Relationship context",
            "",
            "No relationship context supplied by this explicit OSINT command.",
            "",
            "## Open questions",
            "",
        ]
    )
    lines.extend(f"- {question}" for question in open_questions)
    lines.extend(
        [
            "",
            "## Do not assume",
            "",
            "- Do not infer private contact details, sensitive attributes, or relationship status.",
            "- Do not merge people with the same/similar name without source-backed identity match.",
            "- Do not treat search snippets as high confidence without manual verification.",
            "",
            "## Last verified",
            "",
            last_verified_at,
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _summary(
    person_name: str,
    *,
    source_facts: tuple[DossierFact, ...],
    source_links: tuple[SourceLink, ...],
) -> str:
    if not source_facts:
        return (
            f"No source-backed public facts were collected for {person_name}. "
            "Use this dossier as a placeholder until sources are verified."
        )
    return _clean_inline(
        f"Curated public-source dossier for {person_name}. "
        f"Collected {len(source_facts)} low/medium-confidence claims from "
        f"{len(source_links)} public source(s). Verify identity before use.",
        MAX_SUMMARY_CHARS,
    )


def _unknown_or_fact_hint(facts: tuple[DossierFact, ...]) -> str:
    if facts:
        return "See source-backed facts above; no role/company claim is promoted without manual verification."
    return "Unknown from current source set."


def _default_open_questions(person_name: str, *, has_sources: bool) -> tuple[str, ...]:
    base = [
        f"Is this the correct {person_name}, not a namesake?",
        "What is the current role/title and which source verifies it?",
        "Which public profile or company page is authoritative?",
    ]
    if not has_sources:
        base.append("Which public sources should be searched or provided manually?")
    return tuple(base)


def _coerce_source_link(value: SourceLink | Mapping[str, Any] | Any) -> SourceLink:
    if isinstance(value, SourceLink):
        return SourceLink(
            title=_clean_inline(value.title, 140),
            url=_clean_url(value.url),
            description=_clean_inline(value.description, 260),
        )
    if isinstance(value, Mapping):
        return SourceLink(
            title=_clean_inline(str(value.get("title") or ""), 140),
            url=_clean_url(str(value.get("url") or "")),
            description=_clean_inline(str(value.get("description") or ""), 260),
        )
    return SourceLink(
        title=_clean_inline(str(getattr(value, "title", "") or ""), 140),
        url=_clean_url(str(getattr(value, "url", "") or "")),
        description=_clean_inline(str(getattr(value, "description", "") or ""), 260),
    )


def _coerce_fact(value: DossierFact | Mapping[str, Any]) -> DossierFact:
    if isinstance(value, DossierFact):
        return value
    return DossierFact(
        text=str(value.get("text") or value.get("fact") or ""),
        source=str(value.get("source") or ""),
        confidence=str(value.get("confidence") or "low"),
    )


def _coerce_inference(value: DossierInference | Mapping[str, Any] | str) -> DossierInference:
    if isinstance(value, DossierInference):
        return DossierInference(
            text=_clean_inline(value.text, MAX_FACT_CHARS),
            confidence=_normalize_confidence(value.confidence),
            reason=_clean_inline(value.reason, 180),
        )
    if isinstance(value, Mapping):
        return DossierInference(
            text=_clean_inline(str(value.get("text") or value.get("inference") or ""), MAX_FACT_CHARS),
            confidence=_normalize_confidence(str(value.get("confidence") or "low")),
            reason=_clean_inline(str(value.get("reason") or ""), 180),
        )
    return DossierInference(text=_clean_inline(str(value), MAX_FACT_CHARS), confidence="low")


def _normalize_query(query: str) -> str:
    normalized = " ".join(sanitize_text(str(query or "")).split())
    if len(normalized) > 120:
        normalized = normalized[:119] + "…"
    if len(normalized) < 2:
        raise ValueError("person query is too short")
    _validate_public_person_query(normalized)
    return mask_pii(normalized)


def _validate_public_person_query(query: str) -> None:
    if _EMAIL_RE.search(query) or _PHONE_RE.search(query):
        raise ValueError("use a public name/handle, not private contact data")


def _normalize_confidence(value: str) -> str:
    normalized = (value or "low").casefold().strip()
    return normalized if normalized in CONFIDENCE_VALUES else "low"


def _clean_inline(value: str, limit: int) -> str:
    cleaned = mask_pii(" ".join(sanitize_text(str(value or "")).split()))
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _clean_url(value: str) -> str:
    url = _clean_inline(value, limit=400)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return url


def _hostname(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _default_searcher() -> SearchProvider:
    from kronos.tools.brave import search

    return search
