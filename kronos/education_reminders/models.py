"""Pure contracts for safety-gated education reminders."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Set
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1


class AnnouncementStatus(StrEnum):
    """Audit status for scheduled announcements."""

    PREVIEW = "preview"
    SENT = "sent"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class QuietHours:
    """Quiet-hours window in local announcement timezone."""

    start_hour: int = 22
    end_hour: int = 8

    def contains(self, value: datetime, timezone: str) -> bool:
        """Return whether ``value`` falls inside the quiet-hours window."""
        local = value.astimezone(_zoneinfo(timezone))
        hour = local.hour
        if self.start_hour == self.end_hour:
            return False
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(start_hour=int(data.get("start_hour") or 22), end_hour=int(data.get("end_hour") or 8))


@dataclass(frozen=True)
class ScheduledAnnouncement:
    """Outbound announcement configuration with safety gates."""

    target_group_id: int
    message_template: str
    schedule: str
    timezone: str = "UTC"
    idempotency_key: str = ""
    enabled: bool = False
    preview_required: bool = True
    topic_id: int | None = None
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    created_at: str = field(default_factory=_utc_now_iso)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        target_group_id: int,
        message_template: str,
        schedule: str,
        timezone: str = "UTC",
        enabled: bool = False,
        preview_required: bool = True,
        topic_id: int | None = None,
        quiet_hours: QuietHours | None = None,
    ) -> Self:
        """Create an announcement and derive a stable idempotency key."""
        key = compute_idempotency_key(
            target_group_id=target_group_id,
            schedule=schedule,
            message_template=message_template,
            topic_id=topic_id,
        )
        return cls(
            target_group_id=int(target_group_id),
            message_template=message_template.strip(),
            schedule=schedule.strip(),
            timezone=timezone,
            idempotency_key=key,
            enabled=enabled,
            preview_required=preview_required,
            topic_id=topic_id,
            quiet_hours=quiet_hours or QuietHours(),
        )

    def render(self, context: Mapping[str, Any] | None = None) -> str:
        """Render a deterministic preview without LLM generation."""
        values = _SafeFormatDict({str(key): str(value) for key, value in dict(context or {}).items()})
        return self.message_template.format_map(values)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["quiet_hours"] = self.quiet_hours.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            target_group_id=int(data.get("target_group_id") or 0),
            message_template=str(data.get("message_template") or ""),
            schedule=str(data.get("schedule") or ""),
            timezone=str(data.get("timezone") or "UTC"),
            idempotency_key=str(data.get("idempotency_key") or ""),
            enabled=bool(data.get("enabled")),
            preview_required=bool(data.get("preview_required", True)),
            topic_id=int(data["topic_id"]) if data.get("topic_id") is not None else None,
            quiet_hours=QuietHours.from_dict(dict(data.get("quiet_hours") or {})),
            created_at=str(data.get("created_at") or _utc_now_iso()),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class AnnouncementPreview:
    """Dry-run preview shown before the first outbound send."""

    idempotency_key: str
    target_group_id: int
    rendered_text: str
    schedule: str
    timezone: str
    topic_id: int | None = None
    dry_run: bool = True
    created_at: str = field(default_factory=_utc_now_iso)

    @classmethod
    def from_announcement(
        cls,
        announcement: ScheduledAnnouncement,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Self:
        return cls(
            idempotency_key=announcement.idempotency_key,
            target_group_id=announcement.target_group_id,
            rendered_text=announcement.render(context),
            schedule=announcement.schedule,
            timezone=announcement.timezone,
            topic_id=announcement.topic_id,
        )


@dataclass(frozen=True)
class AnnouncementSendRecord:
    """Append-only audit record for a send attempt or dry-run."""

    idempotency_key: str
    target_group_id: int
    status: AnnouncementStatus
    scheduled_for: str
    recorded_at: str = field(default_factory=_utc_now_iso)
    reason: str = ""
    topic_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(self.status)
        return payload


@dataclass(frozen=True)
class SafetyDecision:
    """Result of outbound safety-gate evaluation."""

    can_send: bool
    reason: str


def compute_idempotency_key(
    *,
    target_group_id: int,
    schedule: str,
    message_template: str,
    topic_id: int | None = None,
) -> str:
    """Return a stable idempotency key for one configured announcement."""
    payload = f"{target_group_id}|{topic_id or ''}|{schedule.strip()}|{message_template.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def evaluate_send_gates(
    announcement: ScheduledAnnouncement,
    *,
    allowed_group_ids: Set[int],
    sent_idempotency_keys: Set[str],
    preview_approved: bool,
    dry_run: bool,
    now: datetime,
) -> SafetyDecision:
    """Evaluate outbound safety gates without sending anything."""
    if dry_run:
        return SafetyDecision(False, "dry_run")
    if not announcement.enabled:
        return SafetyDecision(False, "announcement_disabled")
    if announcement.target_group_id not in {int(item) for item in allowed_group_ids}:
        return SafetyDecision(False, "target_group_not_allowlisted")
    if announcement.preview_required and not preview_approved:
        return SafetyDecision(False, "preview_required")
    if announcement.idempotency_key in sent_idempotency_keys:
        return SafetyDecision(False, "idempotency_key_already_sent")
    if announcement.quiet_hours.contains(now, announcement.timezone):
        return SafetyDecision(False, "quiet_hours")
    return SafetyDecision(True, "ok")


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
