"""Data models for competitor monitoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    INFO = "info"


class ChangeType(str, Enum):
    NEW_COMPETITOR = "new_competitor"
    VERSION_UPDATE = "version_update"
    RATING_CHANGE = "rating_change"
    REVIEW_SPIKE = "review_spike"
    DESCRIPTION_CHANGE = "description_change"
    PRICING_CHANGE = "pricing_change"
    # Phase 2 — Web + Social
    WEBSITE_CHANGE = "website_change"
    BLOG_POST = "blog_post"
    SOCIAL_POST = "social_post"
    PRESS_MENTION = "press_mention"
    PRODUCTHUNT_LAUNCH = "producthunt_launch"
    JOB_POSTING = "job_posting"


@dataclass
class CompetitorConfig:
    id: str
    name: str
    tier: int = 2  # 1=daily, 2=weekly, 3=monthly
    ios_id: str = ""
    android_package: str = ""
    website: str = ""
    blog_rss: str = ""
    twitter: str = ""
    linkedin: str = ""


@dataclass
class AppSnapshot:
    """Point-in-time snapshot of an app's public data."""

    version: str = ""
    rating: float = 0.0
    rating_count: int = 0
    release_notes: str = ""
    last_updated: str = ""
    price: float = 0.0
    description: str = ""
    screenshots_count: int = 0
    installs: str = ""  # Play Store only (e.g. "1,000,000+")
    developer: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class Change:
    """Detected change between two snapshots."""

    competitor_id: str
    competitor_name: str
    channel: str
    change_type: ChangeType
    severity: Severity
    summary: str
    details: dict = field(default_factory=dict)
    detected_at: datetime | None = None
