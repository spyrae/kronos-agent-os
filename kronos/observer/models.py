"""Dataclass models for the Observer/Capture Engine.

These models deliberately avoid pydantic or network-specific types. They are
small JSON-friendly contracts shared by capture, scanner, digest, and state
modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self


class ObserverSourceKind(StrEnum):
    """Machine-readable observer input kinds."""

    TELEGRAM_VOICE_NOTE = "telegram_voice_note"
    TELEGRAM_LINK = "telegram_link"
    TELEGRAM_TEXT_CAPTURE = "telegram_text_capture"
    TELEGRAM_UNREAD_DIGEST = "telegram_unread_digest"
    TELEGRAM_REPLY_DEBT = "telegram_reply_debt"
    TELEGRAM_DAILY_SCOPE = "telegram_daily_scope"
    OSINT_PERSON = "osint_person"
    DOCUMENT_CAPTURE = "document_capture"

    @property
    def allows_raw_content(self) -> bool:
        """Return whether storing raw user-provided content is allowed by default."""
        return self in EXPLICIT_CAPTURE_SOURCE_KINDS


EXPLICIT_CAPTURE_SOURCE_KINDS: frozenset[ObserverSourceKind] = frozenset(
    {
        ObserverSourceKind.TELEGRAM_VOICE_NOTE,
        ObserverSourceKind.TELEGRAM_LINK,
        ObserverSourceKind.TELEGRAM_TEXT_CAPTURE,
        ObserverSourceKind.OSINT_PERSON,
        ObserverSourceKind.DOCUMENT_CAPTURE,
    }
)


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp for observer JSON files."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _source_kind(value: ObserverSourceKind | str) -> ObserverSourceKind:
    if isinstance(value, ObserverSourceKind):
        return value
    return ObserverSourceKind(str(value))


class ObserverModel:
    """Minimal pydantic-like helpers shared by observer dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return _json_safe(asdict(self))


@dataclass(frozen=True)
class CapturedItem(ObserverModel):
    """Explicit user capture that may preserve raw content locally."""

    content: str
    source_kind: ObserverSourceKind = ObserverSourceKind.TELEGRAM_TEXT_CAPTURE
    item_id: str = ""
    captured_at: str = field(default_factory=utc_now_iso)
    source_peer_id: str = ""
    source_peer_title: str = ""
    source_message_id: int | None = None
    content_excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        payload = dict(data)
        payload["source_kind"] = _source_kind(payload.get("source_kind", ObserverSourceKind.TELEGRAM_TEXT_CAPTURE))
        payload["metadata"] = _metadata(payload.get("metadata"))
        return cls(**payload)


@dataclass(frozen=True)
class BookmarkCandidate(ObserverModel):
    """URL candidate extracted from an explicit capture or dialog summary."""

    url: str
    title: str = ""
    source_kind: ObserverSourceKind = ObserverSourceKind.TELEGRAM_LINK
    source_item_id: str = ""
    captured_at: str = field(default_factory=utc_now_iso)
    source_peer_id: str = ""
    source_peer_title: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        payload = dict(data)
        payload["source_kind"] = _source_kind(payload.get("source_kind", ObserverSourceKind.TELEGRAM_LINK))
        payload["tags"] = _string_tuple(payload.get("tags"))
        payload["metadata"] = _metadata(payload.get("metadata"))
        return cls(**payload)


@dataclass(frozen=True)
class DialogSnapshot(ObserverModel):
    """Safe, summarized read-only view of a Telegram dialog."""

    peer_id: str
    source_kind: ObserverSourceKind = ObserverSourceKind.TELEGRAM_UNREAD_DIGEST
    peer_title: str = ""
    captured_at: str = field(default_factory=utc_now_iso)
    last_message_id: int | None = None
    unread_count: int = 0
    message_count: int = 0
    summary: str = ""
    excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        payload = dict(data)
        payload["source_kind"] = _source_kind(payload.get("source_kind", ObserverSourceKind.TELEGRAM_UNREAD_DIGEST))
        payload["metadata"] = _metadata(payload.get("metadata"))
        return cls(**payload)


@dataclass(frozen=True)
class ReplyDebt(ObserverModel):
    """A dialog where the user probably owes a reply."""

    peer_id: str
    source_kind: ObserverSourceKind = ObserverSourceKind.TELEGRAM_REPLY_DEBT
    peer_title: str = ""
    detected_at: str = field(default_factory=utc_now_iso)
    last_incoming_at: str = ""
    last_incoming_message_id: int | None = None
    last_incoming_excerpt: str = ""
    hours_waiting: float = 0.0
    severity: str = "medium"
    reason: str = ""
    suggested_action: str = ""
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        payload = dict(data)
        payload["source_kind"] = _source_kind(payload.get("source_kind", ObserverSourceKind.TELEGRAM_REPLY_DEBT))
        payload["metadata"] = _metadata(payload.get("metadata"))
        return cls(**payload)


@dataclass(frozen=True)
class DailyScopeEntry(ObserverModel):
    """One safe evening summary entry for a dialog or person."""

    peer_id: str
    summary: str
    source_kind: ObserverSourceKind = ObserverSourceKind.TELEGRAM_DAILY_SCOPE
    peer_title: str = ""
    happened_at: str = ""
    captured_at: str = field(default_factory=utc_now_iso)
    excerpt: str = ""
    topics: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        payload = dict(data)
        payload["source_kind"] = _source_kind(payload.get("source_kind", ObserverSourceKind.TELEGRAM_DAILY_SCOPE))
        payload["topics"] = _string_tuple(payload.get("topics"))
        payload["metadata"] = _metadata(payload.get("metadata"))
        return cls(**payload)


@dataclass(frozen=True)
class ObserverRunResult(ObserverModel):
    """Append-only metadata for one observer run."""

    source_kind: ObserverSourceKind
    run_id: str = ""
    status: str = "completed"
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    scanned_count: int = 0
    captured_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        payload = dict(data)
        payload["source_kind"] = _source_kind(payload["source_kind"])
        payload["errors"] = _string_tuple(payload.get("errors"))
        payload["metadata"] = _metadata(payload.get("metadata"))
        return cls(**payload)
