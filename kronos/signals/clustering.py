"""Deterministic deduplication helpers for signal items."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from kronos.signals.models import SignalItem
from kronos.signals.scoring import origin_key


@dataclass(frozen=True)
class DeduplicationResult:
    """Result of deterministic item deduplication."""

    unique_items: tuple[SignalItem, ...]
    duplicate_indexes: dict[int, int]


def deduplicate_items(items: list[SignalItem] | tuple[SignalItem, ...]) -> DeduplicationResult:
    """Deduplicate by URL/origin first, then by normalized text fingerprint."""
    origin_to_index: dict[str, int] = {}
    fingerprint_to_index: dict[str, int] = {}
    duplicates: dict[int, int] = {}
    unique: list[SignalItem] = []

    for index, item in enumerate(items):
        origin = origin_key(item)
        fingerprint = item_fingerprint(item)
        duplicate_of = origin_to_index.get(origin)
        if duplicate_of is None:
            duplicate_of = fingerprint_to_index.get(fingerprint)
        if duplicate_of is not None:
            duplicates[index] = duplicate_of
            continue

        unique_index = len(unique)
        origin_to_index[origin] = unique_index
        fingerprint_to_index[fingerprint] = unique_index
        unique.append(item)

    return DeduplicationResult(unique_items=tuple(unique), duplicate_indexes=duplicates)


def item_fingerprint(item: SignalItem) -> str:
    """Return a content fingerprint stable across reposts and source snippets."""
    text = item.normalized_text or item.text or item.title or item.url or item.source_item_key
    tokens = re.findall(r"[a-zа-я0-9]+", text.lower())
    normalized = " ".join(tokens[:80])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
