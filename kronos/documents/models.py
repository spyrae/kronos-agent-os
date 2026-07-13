"""Pure contracts for the future Documents Collector pipeline."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

SCHEMA_VERSION = 1
CONFIDENT_THRESHOLD = 0.8


class DocumentSourceKind(StrEnum):
    """Supported document ingestion sources."""

    TELEGRAM_ATTACHMENT = "telegram_attachment"
    EMAIL_ATTACHMENT = "email_attachment"


class DocumentType(StrEnum):
    """Document classes for conservative routing."""

    CONTRACT = "contract"
    INVOICE = "invoice"
    ACT = "act"
    RECEIPT = "receipt"
    PDF = "pdf"
    DOCX = "docx"
    IMAGE = "image"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    """Manual review state for stored document candidates."""

    QUARANTINED = "quarantined"
    STORED = "stored"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(content: bytes) -> str:
    """Return a sha256 checksum for document bytes."""
    return hashlib.sha256(content).hexdigest()


def normalize_filename(filename: str, *, default: str = "document") -> str:
    """Return a path-safe filename while preserving a useful extension."""
    name = Path(str(filename or "")).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        name = default
    stem = Path(name).stem or default
    suffix = Path(name).suffix.lower()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_") or default
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:16]
    return f"{safe_stem[:96]}{safe_suffix}"


def normalize_project_slug(value: str, *, default: str = "unassigned") -> str:
    """Return a path-safe project slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return slug[:80] or default


@dataclass(frozen=True)
class DocumentCandidate:
    """Attachment candidate before classification or storage."""

    source_kind: DocumentSourceKind
    source_id: str
    filename: str
    checksum_sha256: str
    content_type: str = ""
    size_bytes: int = 0
    detected_at: str = field(default_factory=utc_now_iso)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_bytes(
        cls,
        *,
        source_kind: DocumentSourceKind | str,
        source_id: str,
        filename: str,
        content: bytes,
        content_type: str = "",
        detected_at: str | None = None,
        source_metadata: Mapping[str, Any] | None = None,
    ) -> Self:
        """Create a candidate with checksum and normalized filename."""
        return cls(
            source_kind=DocumentSourceKind(str(source_kind)),
            source_id=str(source_id),
            filename=normalize_filename(filename),
            checksum_sha256=sha256_bytes(content),
            content_type=str(content_type or ""),
            size_bytes=len(content),
            detected_at=detected_at or utc_now_iso(),
            source_metadata=dict(source_metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""
        payload = asdict(self)
        payload["source_kind"] = str(self.source_kind)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Load a candidate from serialized state."""
        return cls(
            source_kind=DocumentSourceKind(str(data["source_kind"])),
            source_id=str(data.get("source_id") or ""),
            filename=normalize_filename(str(data.get("filename") or "document")),
            checksum_sha256=str(data.get("checksum_sha256") or ""),
            content_type=str(data.get("content_type") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            detected_at=str(data.get("detected_at") or utc_now_iso()),
            source_metadata=dict(data.get("source_metadata") or {}),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class DocumentClassification:
    """Conservative document type classification."""

    document_type: DocumentType = DocumentType.UNKNOWN
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()
    extracted_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_confident(self) -> bool:
        """Return whether the document may skip quarantine for type confidence."""
        return self.document_type != DocumentType.UNKNOWN and self.confidence >= CONFIDENT_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["document_type"] = str(self.document_type)
        payload["reasons"] = list(self.reasons)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            document_type=DocumentType(str(data.get("document_type") or DocumentType.UNKNOWN)),
            confidence=float(data.get("confidence") or 0.0),
            reasons=tuple(str(item) for item in data.get("reasons") or ()),
            extracted_metadata=dict(data.get("extracted_metadata") or {}),
        )


@dataclass(frozen=True)
class ProjectMatch:
    """Candidate project/company routing decision."""

    project_slug: str = ""
    company: str = ""
    confidence: float = 0.0
    reason: str = ""

    @property
    def is_confident(self) -> bool:
        """Return whether the project match may skip quarantine."""
        return bool(self.project_slug) and self.confidence >= CONFIDENT_THRESHOLD

    def normalized_slug(self) -> str:
        """Return the path-safe project slug."""
        return normalize_project_slug(self.project_slug)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            project_slug=normalize_project_slug(str(data.get("project_slug") or ""), default=""),
            company=str(data.get("company") or ""),
            confidence=float(data.get("confidence") or 0.0),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True)
class StoredDocument:
    """Index entry for a stored or quarantined document."""

    candidate: DocumentCandidate
    classification: DocumentClassification
    project_match: ProjectMatch
    storage_path: str
    index_note_path: str = ""
    review_status: ReviewStatus = ReviewStatus.QUARANTINED
    stored_at: str = field(default_factory=utc_now_iso)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable index payload."""
        return {
            "candidate": self.candidate.to_dict(),
            "classification": self.classification.to_dict(),
            "project_match": self.project_match.to_dict(),
            "storage_path": self.storage_path,
            "index_note_path": self.index_note_path,
            "review_status": str(self.review_status),
            "stored_at": self.stored_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Load a stored document index entry."""
        return cls(
            candidate=DocumentCandidate.from_dict(dict(data["candidate"])),
            classification=DocumentClassification.from_dict(dict(data["classification"])),
            project_match=ProjectMatch.from_dict(dict(data["project_match"])),
            storage_path=str(data.get("storage_path") or ""),
            index_note_path=str(data.get("index_note_path") or ""),
            review_status=ReviewStatus(str(data.get("review_status") or ReviewStatus.QUARANTINED)),
            stored_at=str(data.get("stored_at") or utc_now_iso()),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        )


def should_quarantine(classification: DocumentClassification, project_match: ProjectMatch) -> bool:
    """Return whether a candidate must stay in quarantine."""
    return not (classification.is_confident and project_match.is_confident)


def planned_storage_path(
    candidate: DocumentCandidate,
    classification: DocumentClassification,
    project_match: ProjectMatch,
    *,
    workspace_root: Path,
) -> Path:
    """Return the intended safe storage path without touching the filesystem."""
    filename = f"{candidate.checksum_sha256[:12]}-{normalize_filename(candidate.filename)}"
    root = Path(workspace_root)
    if should_quarantine(classification, project_match):
        return root / "ops" / "documents" / "quarantine" / filename
    return root / "notes" / "world" / "projects" / project_match.normalized_slug() / "documents" / filename
