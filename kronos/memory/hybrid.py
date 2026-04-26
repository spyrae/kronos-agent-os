"""Hybrid search — merges vector (Qdrant/Mem0) and keyword (FTS5) results.

Two search paths run in parallel:
1. Vector search via Mem0 → semantic similarity (good for meaning)
2. FTS5 search → keyword match (good for names, dates, IDs, numbers)

Results are merged with score normalization and MMR re-ranking.
"""

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("kronos.memory.hybrid")

# Merge weights
VECTOR_WEIGHT = 0.7
TEXT_WEIGHT = 0.3

# MMR: balance relevance vs diversity (0=pure diversity, 1=pure relevance)
MMR_LAMBDA = 0.7

# Temporal decay half-life in days
DECAY_HALF_LIFE = 60


def merge_hybrid_results(
    vector_results: list[dict],
    fts_results: list[dict],
    limit: int = 5,
    vector_weight: float = VECTOR_WEIGHT,
    text_weight: float = TEXT_WEIGHT,
) -> list[str]:
    """Merge vector and FTS5 results into a single ranked list.

    Args:
        vector_results: From Mem0 search — list of {"memory": str, "score": float, "created_at": str}
        fts_results: From FTS5 search — list of {"content": str, "rank": float, "created_at": str}
        limit: Max results to return.
        vector_weight: Weight for vector scores (0-1).
        text_weight: Weight for FTS scores (0-1).

    Returns:
        List of memory strings, ranked by hybrid score.
    """
    # Normalize vector scores (already 0-1 from cosine similarity)
    vector_entries = []
    for item in vector_results:
        text = item.get("memory", "")
        if not text:
            continue
        vector_entries.append({
            "text": text,
            "vector_score": item.get("score", 0.0),
            "fts_score": 0.0,
            "created_at": item.get("created_at"),
        })

    # Normalize FTS5 ranks (negative, lower = better) → 0-1 scale
    fts_entries = []
    if fts_results:
        # FTS5 rank is negative BM25, normalize to 0-1
        raw_ranks = [abs(r.get("rank", 0)) for r in fts_results]
        max_rank = max(raw_ranks) if raw_ranks else 1.0
        if max_rank == 0:
            max_rank = 1.0

        for item in fts_results:
            text = item.get("content", "")
            if not text:
                continue
            normalized = abs(item.get("rank", 0)) / max_rank
            fts_entries.append({
                "text": text,
                "vector_score": 0.0,
                "fts_score": normalized,
                "created_at": item.get("created_at"),
            })

    # Merge by text content (union with score combination)
    merged: dict[str, dict] = {}

    for entry in vector_entries:
        key = _normalize_key(entry["text"])
        if key in merged:
            merged[key]["vector_score"] = max(merged[key]["vector_score"], entry["vector_score"])
        else:
            merged[key] = entry.copy()

    for entry in fts_entries:
        key = _normalize_key(entry["text"])
        if key in merged:
            merged[key]["fts_score"] = max(merged[key]["fts_score"], entry["fts_score"])
        else:
            merged[key] = entry.copy()

    # Compute hybrid score with temporal decay
    now = datetime.now(timezone.utc)
    for entry in merged.values():
        base_score = (
            entry["vector_score"] * vector_weight
            + entry["fts_score"] * text_weight
        )

        # Boost items found by both methods
        if entry["vector_score"] > 0 and entry["fts_score"] > 0:
            base_score *= 1.2  # 20% boost for agreement

        # Apply temporal decay
        decay = _temporal_decay(entry.get("created_at"), now)
        entry["hybrid_score"] = base_score * decay

    # Sort by hybrid score and apply MMR
    candidates = sorted(merged.values(), key=lambda x: x["hybrid_score"], reverse=True)
    selected = _mmr_select(candidates, limit)

    return [entry["text"] for entry in selected]


def _normalize_key(text: str) -> str:
    """Normalize text for deduplication (lowercase, strip whitespace)."""
    return text.strip().lower()[:200]


def _temporal_decay(created_at: str | None, now: datetime) -> float:
    """Compute temporal decay factor. Score = 2^(-age_days / half_life)."""
    if not created_at:
        return 1.0
    try:
        if isinstance(created_at, str):
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        else:
            return 1.0
        age_days = (now - created).total_seconds() / 86400
        return math.pow(2, -age_days / DECAY_HALF_LIFE)
    except (ValueError, TypeError):
        return 1.0


def _mmr_select(candidates: list[dict], limit: int) -> list[dict]:
    """Maximal Marginal Relevance — balance relevance and diversity.

    Greedy selection: at each step pick the candidate that maximizes
    λ * relevance - (1-λ) * max_similarity_to_selected.

    Uses simple word overlap as similarity proxy (no extra embeddings needed).
    """
    if len(candidates) <= limit:
        return candidates

    selected: list[dict] = []
    remaining = list(candidates)

    # Always take the top candidate
    selected.append(remaining.pop(0))

    while len(selected) < limit and remaining:
        best_idx = 0
        best_mmr = -float("inf")

        for i, cand in enumerate(remaining):
            relevance = cand["hybrid_score"]

            # Max similarity to any already-selected item
            max_sim = max(
                _word_overlap(cand["text"], s["text"])
                for s in selected
            )

            mmr_score = MMR_LAMBDA * relevance - (1 - MMR_LAMBDA) * max_sim
            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        selected.append(remaining.pop(best_idx))

    return selected


def _word_overlap(text_a: str, text_b: str) -> float:
    """Simple word overlap similarity (Jaccard-like). Returns 0-1."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
