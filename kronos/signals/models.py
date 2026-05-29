"""Data models for the Signal Intelligence store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    """Return a compact UTC timestamp for SQLite storage."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SignalItem:
    """Normalized source item before clustering and digest rendering."""

    source_id: str
    source_platform: str
    title: str = ""
    text: str = ""
    url: str = ""
    source_item_key: str = ""
    source_url: str = ""
    author: str = ""
    handle: str = ""
    published_at: str = ""
    fetched_at: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
    normalized_text: str = ""
    categories: tuple[str, ...] = ()
    importance_score: float = 0.0
    confidence_score: float = 0.0
    evidence_level: str = "observation"
    cluster_id: int | None = None
    duplicate_of: int | None = None


@dataclass(frozen=True)
class SignalCluster:
    """Cluster of related signal items."""

    category: str
    title: str
    summary: str = ""
    evidence_level: str = "observation"
    item_ids: tuple[int, ...] = ()
    source_ids: tuple[str, ...] = ()
    platform_ids: tuple[str, ...] = ()
    evidence_count: int = 0
    source_count: int = 0
    platform_count: int = 0
    importance_score: float = 0.0
    confidence_score: float = 0.0
    first_seen_at: str = ""
    last_seen_at: str = ""


@dataclass(frozen=True)
class SignalDigest:
    """Rendered digest metadata and body."""

    destination: str
    title: str
    body: str
    categories: tuple[str, ...] = ()
    item_ids: tuple[int, ...] = ()
    cluster_ids: tuple[int, ...] = ()
    generated_at: str = ""
    sent_at: str = ""


@dataclass(frozen=True)
class StoreWriteResult:
    """Result of an idempotent store write."""

    id: int
    inserted: bool
    duplicate_of: int | None = None
