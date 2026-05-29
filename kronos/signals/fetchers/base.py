"""Common fetcher types and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kronos.signals.models import SignalItem
from kronos.signals.sources import SignalSource


class FetchErrorKind(StrEnum):
    """Machine-readable fetch failure categories."""

    SOURCE_UNAVAILABLE = "source_unavailable"
    AUTH_MISSING = "auth_missing"
    PARSER_FAILURE = "parser_failure"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


@dataclass(frozen=True)
class FetchError:
    """A categorized source fetch error."""

    kind: FetchErrorKind
    source_id: str
    message: str


class FetcherError(RuntimeError):
    """Internal exception used to convert failures into FetchResult errors."""

    def __init__(self, kind: FetchErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class FetchResult:
    """Structured output from one source fetch."""

    source: SignalSource
    items: tuple[SignalItem, ...] = ()
    errors: tuple[FetchError, ...] = ()
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class FetchOptions:
    """Runtime controls shared by fetchers."""

    limit: int = 10
    freshness: str = "pd"


def error_result(source: SignalSource, kind: FetchErrorKind, message: str, elapsed_ms: int = 0) -> FetchResult:
    return FetchResult(
        source=source,
        errors=(FetchError(kind=kind, source_id=source.id, message=message),),
        elapsed_ms=elapsed_ms,
    )


def source_item(
    source: SignalSource,
    *,
    title: str = "",
    text: str = "",
    url: str = "",
    source_item_key: str = "",
    source_url: str = "",
    author: str = "",
    handle: str = "",
    published_at: str = "",
    raw_payload: dict | None = None,
    importance_score: float = 0.0,
    confidence_score: float = 0.0,
    evidence_level: str = "observation",
) -> SignalItem:
    """Build a normalized SignalItem from a source adapter."""
    return SignalItem(
        source_id=source.id,
        source_platform=source.platform,
        source_item_key=source_item_key,
        source_url=source_url,
        author=author,
        handle=handle or source.handle,
        title=_compact(title),
        text=_compact(text),
        url=url,
        published_at=published_at,
        raw_payload=raw_payload or {},
        normalized_text=_compact("\n".join(part for part in (title, text) if part)),
        categories=source.categories,
        importance_score=float(importance_score),
        confidence_score=float(confidence_score),
        evidence_level=evidence_level,
    )


def _compact(text: str) -> str:
    return " ".join((text or "").split())
