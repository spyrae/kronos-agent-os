"""Pure capture classification and persistence helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kronos.agents.knowledge_pipeline.queue import KnowledgeQueue
from kronos.observer.models import ObserverSourceKind
from kronos.workspace import Workspace

FORCED_CAPTURE_PREFIXES = ("запомни:", "сохрани:", "note:", "capture:")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()]+")
TRAILING_URL_PUNCTUATION = ".,!?;:)]}»”"
AGENT_REQUEST_MARKERS = (
    "?",
    "что думаешь",
    "что скажешь",
    "как думаешь",
    "объясни",
    "прочитай",
    "перескажи",
    "суммаризируй",
    "проанализируй",
    "найди",
    "сравни",
    "сделай",
    "можешь",
    "посмотри",
    "what do you think",
    "summarize",
    "analyse",
    "analyze",
    "explain",
    "compare",
    "find",
)


@dataclass(frozen=True)
class CaptureDecision:
    """Deterministic result of classifying an incoming DM payload."""

    should_capture: bool
    source_kind: ObserverSourceKind | None = None
    content: str = ""
    urls: tuple[str, ...] = ()
    original_modality: str = "text"
    reason: str = ""

    @property
    def source_kind_value(self) -> str:
        """Return the source kind as a stable string for metadata/task files."""
        return self.source_kind.value if self.source_kind else ""


def extract_urls(text: str) -> list[str]:
    """Extract HTTP(S)/www URLs from text, preserving first-seen order."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def is_forced_capture(text: str) -> bool:
    """Return whether text starts with an explicit capture prefix."""
    normalized = (text or "").lstrip().casefold()
    return any(normalized.startswith(prefix) for prefix in FORCED_CAPTURE_PREFIXES)


def strip_forced_capture_prefix(text: str) -> str:
    """Remove a forced capture prefix while preserving the actual note body."""
    stripped = (text or "").lstrip()
    normalized = stripped.casefold()
    for prefix in FORCED_CAPTURE_PREFIXES:
        if normalized.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped.strip()


def classify_capture(
    clean_text: str,
    is_voice: bool,
    is_dm: bool,
    has_image: bool = False,
) -> CaptureDecision:
    """Classify whether an incoming normalized Telegram payload is a capture.

    The function has no Telegram, LLM, filesystem, or network side effects.
    """
    text = (clean_text or "").strip()
    urls = tuple(extract_urls(text))
    modality = _modality(is_voice=is_voice, has_image=has_image, urls=urls)

    if not is_dm:
        return CaptureDecision(False, urls=urls, original_modality=modality, reason="not_dm")
    if has_image:
        return CaptureDecision(False, urls=urls, original_modality=modality, reason="image_unsupported")
    if not text:
        return CaptureDecision(False, urls=urls, original_modality=modality, reason="empty")
    if is_voice:
        return CaptureDecision(
            True,
            source_kind=ObserverSourceKind.TELEGRAM_VOICE_NOTE,
            content=text,
            urls=urls,
            original_modality="voice",
            reason="dm_voice_note",
        )
    if is_forced_capture(text):
        content = strip_forced_capture_prefix(text)
        if not content:
            return CaptureDecision(False, urls=urls, original_modality=modality, reason="empty_forced_capture")
        return CaptureDecision(
            True,
            source_kind=ObserverSourceKind.TELEGRAM_TEXT_CAPTURE,
            content=content,
            urls=urls,
            original_modality="text",
            reason="forced_capture",
        )
    if urls and _is_standalone_url_text(text, urls):
        return CaptureDecision(
            True,
            source_kind=ObserverSourceKind.TELEGRAM_LINK,
            content=text,
            urls=urls,
            original_modality="link",
            reason="standalone_link",
        )
    if urls and _looks_like_agent_request(text):
        return CaptureDecision(False, urls=urls, original_modality="text", reason="url_agent_request")

    return CaptureDecision(False, urls=urls, original_modality=modality, reason="ordinary_message")


def record_capture(
    clean_text: str,
    *,
    is_voice: bool,
    is_dm: bool,
    has_image: bool = False,
    chat_id: str | int | None = None,
    user_id: str | int | None = None,
    message_id: str | int | None = None,
    timestamp: str = "",
    workspace: Workspace | None = None,
    queue: KnowledgeQueue | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record a classified capture via ``KnowledgeQueue``.

    Returns the knowledge task when a capture is recorded, otherwise ``None``.
    """
    decision = classify_capture(
        clean_text,
        is_voice=is_voice,
        is_dm=is_dm,
        has_image=has_image,
    )
    if not decision.should_capture or not decision.source_kind:
        return None

    metadata = _metadata(
        decision,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        timestamp=timestamp,
        extra_metadata=extra_metadata,
    )
    knowledge_queue = queue or KnowledgeQueue(workspace)
    return knowledge_queue.record_source(decision.source_kind.value, decision.content, metadata=metadata)


def _modality(*, is_voice: bool, has_image: bool, urls: tuple[str, ...]) -> str:
    if is_voice:
        return "voice"
    if has_image:
        return "image"
    if urls:
        return "link"
    return "text"


def _is_standalone_url_text(text: str, urls: tuple[str, ...]) -> bool:
    remainder = text
    for url in urls:
        remainder = remainder.replace(url, " ")
    return re.sub(r"[\s,.;:!?()[\]{}<>«»\"'`~*_—–-]+", "", remainder) == ""


def _looks_like_agent_request(text: str) -> bool:
    normalized = " ".join((text or "").casefold().split())
    return any(marker in normalized for marker in AGENT_REQUEST_MARKERS)


def _metadata(
    decision: CaptureDecision,
    *,
    chat_id: str | int | None,
    user_id: str | int | None,
    message_id: str | int | None,
    timestamp: str,
    extra_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source_kind": decision.source_kind_value,
        "urls": list(decision.urls),
        "original_modality": decision.original_modality,
        "classification_reason": decision.reason,
    }
    if chat_id is not None:
        metadata["chat_id"] = chat_id
    if user_id is not None:
        metadata["user_id"] = user_id
    if message_id is not None:
        metadata["message_id"] = message_id
    if timestamp:
        metadata["timestamp"] = timestamp
    if extra_metadata:
        metadata.update(dict(extra_metadata))
    return metadata
