"""Bookmark sink contracts for Observer link captures."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit


class BookmarkStatus(StrEnum):
    """Bookmark persistence result categories."""

    SAVED = "saved"
    DUPLICATE = "duplicate"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


@dataclass(frozen=True)
class BookmarkResult:
    """Result of saving one normalized bookmark URL."""

    status: BookmarkStatus
    url: str
    error: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"status": self.status.value, "url": self.url}
        if self.error:
            payload["error"] = self.error
        return payload


class BookmarkSink(Protocol):
    """Interface for optional bookmark sinks such as Raindrop."""

    def save(self, url: str, *, metadata: Mapping[str, Any] | None = None) -> BookmarkResult:
        """Persist a normalized URL and return a non-raising result."""


class NoopBookmarkSink:
    """Safe default sink when no external bookmark provider is configured."""

    def save(self, url: str, *, metadata: Mapping[str, Any] | None = None) -> BookmarkResult:
        return BookmarkResult(BookmarkStatus.NOT_CONFIGURED, url)


class RaindropBookmarkSink:
    """Optional Raindrop sink stub.

    The direct API path should use ``RAINDROP_API_TOKEN`` from the environment
    when it is implemented. Until then this class is intentionally non-networked:
    local capture must keep working and tokens must never be logged or required.
    """

    def __init__(self, token: str | None = None):
        self._token = token if token is not None else os.environ.get("RAINDROP_API_TOKEN", "")

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def save(self, url: str, *, metadata: Mapping[str, Any] | None = None) -> BookmarkResult:
        if not self.configured:
            return BookmarkResult(BookmarkStatus.NOT_CONFIGURED, url)
        return BookmarkResult(
            BookmarkStatus.FAILED,
            url,
            error="raindrop sink is configured but direct API persistence is not enabled",
        )


def normalize_url(url: str) -> str:
    """Normalize a URL for per-message deduplication and sink metadata."""
    clean = (url or "").strip().rstrip(".,!?;:)]}»”")
    if not clean:
        return ""
    if clean.casefold().startswith("www."):
        clean = f"https://{clean}"

    parts = urlsplit(clean)
    if not parts.scheme or not parts.netloc:
        return clean

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    return urlunsplit((scheme, netloc, parts.path or "", parts.query, ""))


def save_bookmarks(
    urls: list[str] | tuple[str, ...],
    *,
    sink: BookmarkSink | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> list[BookmarkResult]:
    """Save unique normalized URLs while marking duplicates in the result list."""
    active_sink = sink or NoopBookmarkSink()
    results: list[BookmarkResult] = []
    seen: set[str] = set()

    for raw_url in urls:
        url = normalize_url(raw_url)
        if not url:
            continue
        if url in seen:
            results.append(BookmarkResult(BookmarkStatus.DUPLICATE, url))
            continue
        seen.add(url)
        try:
            result = active_sink.save(url, metadata=metadata)
        except Exception as e:
            result = BookmarkResult(BookmarkStatus.FAILED, url, error=str(e))
        results.append(result)

    return results
