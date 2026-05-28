"""Deterministic nodes for the knowledge pipeline."""

from __future__ import annotations

import re
from typing import Any

from kronos.agents.knowledge_pipeline.queue import KnowledgeQueue, validate_task_schema

_WIKI_LINK_RE = re.compile(r"\[\[([^\]\n]{2,120})\]\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_ENTITY_RE = re.compile(
    r"\b(?:[A-ZА-ЯЁ][\wА-Яа-яЁё0-9-]+(?:\s+[A-ZА-ЯЁ][\wА-Яа-яЁё0-9-]+){0,4}|[A-Z0-9]{2,}(?:-[A-Z0-9]+)*)\b"
)
_STOP_ENTITIES = {
    "A",
    "An",
    "And",
    "But",
    "For",
    "If",
    "In",
    "Or",
    "The",
    "This",
    "To",
    "When",
    "Где",
    "Для",
    "Если",
    "Как",
    "Что",
    "Это",
}


def process_claims(queue: KnowledgeQueue, task: dict[str, Any]) -> dict[str, Any]:
    """Extract candidate claims from the inbox record."""
    text = queue.read_inbox(task)
    claims = [
        {
            "id": f"claim_{index:03d}",
            "text": claim,
            "source": {"inbox_path": task["inbox_path"]},
            "links": [],
        }
        for index, claim in enumerate(extract_claims(text), start=1)
    ]
    task["claims"] = claims
    task["state"] = "processed"
    return queue.mark_phase(task, "process", "completed", {"claims": len(claims)})


def connect_claims(queue: KnowledgeQueue, task: dict[str, Any]) -> dict[str, Any]:
    """Attach wiki-link candidates to each extracted claim."""
    all_links: dict[str, dict[str, str]] = {}
    for claim in task.get("claims", []):
        links = build_claim_links(str(claim.get("text", "")))
        claim["links"] = links
        for link in links:
            all_links[link["wiki"]] = link

    task["links"] = sorted(all_links.values(), key=lambda item: item["wiki"].lower())
    task["state"] = "connected"
    return queue.mark_phase(task, "connect", "completed", {"links": len(task["links"])})


def verify_task(queue: KnowledgeQueue, task: dict[str, Any]) -> dict[str, Any]:
    """Verify task schema, claims, wiki links, and orphan claims."""
    errors = validate_task_schema(task)
    warnings: list[str] = []
    orphan_claims: list[str] = []

    claims = task.get("claims", [])
    if not claims:
        warnings.append("no claims extracted")

    for claim in claims:
        if not str(claim.get("text", "")).strip():
            errors.append(f"{claim.get('id', 'claim')}: empty text")
        links = claim.get("links", [])
        if not links:
            orphan_claims.append(str(claim.get("id") or "unknown"))
        for link in links:
            wiki = str(link.get("wiki") or "")
            if not _WIKI_LINK_RE.fullmatch(wiki):
                errors.append(f"{claim.get('id', 'claim')}: invalid wiki link {wiki!r}")

    if orphan_claims:
        warnings.append(f"{len(orphan_claims)} orphan claim(s) without links")

    status = "failed" if errors else "needs_review" if orphan_claims else "verified"
    task["state"] = status
    task["verification"] = {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "orphan_claims": orphan_claims,
        "claims": len(claims),
        "links": len(task.get("links", [])),
    }
    return queue.mark_phase(task, "verify", "completed" if not errors else "failed", task["verification"])


def sync_claims_to_memory(
    queue: KnowledgeQueue,
    task: dict[str, Any],
    *,
    user_id: str = "knowledge-pipeline",
) -> dict[str, Any]:
    """Mirror verified claims to Mem0/FTS through the existing memory store."""
    if task.get("state") == "failed":
        task["memory"] = {"status": "skipped", "reason": "verification failed"}
        return queue.mark_phase(task, "memory", "skipped", task["memory"])

    claim_texts = [str(claim.get("text") or "").strip() for claim in task.get("claims", [])]
    claim_texts = [text for text in claim_texts if text]
    if not claim_texts:
        task["memory"] = {"status": "skipped", "reason": "no claims"}
        return queue.mark_phase(task, "memory", "skipped", task["memory"])

    try:
        from kronos.memory.store import add_memories

        facts = add_memories(
            [{"role": "user", "content": "\n".join(f"- {claim}" for claim in claim_texts)}],
            user_id=user_id,
            session_id=str(task["task_id"]),
        )
    except Exception as exc:  # pragma: no cover - add_memories normally handles provider failures.
        task["memory"] = {"status": "failed", "error": str(exc)}
        return queue.mark_phase(task, "memory", "failed", task["memory"], error=str(exc))

    task["memory"] = {
        "status": "stored" if facts else "attempted_no_facts",
        "claims_attempted": len(claim_texts),
        "facts_returned": len(facts),
    }
    if task["state"] == "verified":
        task["state"] = "completed"
    return queue.mark_phase(task, "memory", "completed", task["memory"])


def run_pipeline(
    queue: KnowledgeQueue,
    task: dict[str, Any] | str,
    *,
    sync_memory: bool = True,
    memory_user_id: str = "knowledge-pipeline",
) -> dict[str, Any]:
    """Run all knowledge phases, reloading the task file between steps."""
    if isinstance(task, str):
        current = queue.load_task(task)
    else:
        current = queue.load_task(str(task["task_id"]))

    for node in (process_claims, connect_claims, verify_task):
        current = node(queue, current)
        queue.save_task(current)
        current = queue.load_task(str(current["task_id"]))

    if sync_memory:
        current = sync_claims_to_memory(queue, current, user_id=memory_user_id)
        queue.save_task(current)
        current = queue.load_task(str(current["task_id"]))

    return current


def extract_claims(text: str, *, limit: int = 20) -> list[str]:
    """Extract concise claim candidates from markdown/plain text."""
    candidates: list[str] = []
    for chunk in _SENTENCE_RE.split(text):
        cleaned = _clean_claim(chunk)
        if not cleaned:
            continue
        if len(cleaned.split()) < 3 and not _WIKI_LINK_RE.search(cleaned):
            continue
        if cleaned not in candidates:
            candidates.append(cleaned)
        if len(candidates) >= limit:
            break
    return candidates


def build_claim_links(text: str) -> list[dict[str, str]]:
    """Build wiki-link records for an extracted claim."""
    targets: list[str] = []
    for explicit in _WIKI_LINK_RE.findall(text):
        target = explicit.strip()
        if target and target not in targets:
            targets.append(target)

    for match in _ENTITY_RE.findall(text):
        target = match.strip()
        if target in _STOP_ENTITIES or target.isdigit() or target in targets:
            continue
        targets.append(target)

    return [
        {
            "target": target,
            "wiki": f"[[{target}]]",
            "type": "entity",
        }
        for target in targets[:12]
    ]


def _clean_claim(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
    cleaned = re.sub(r"^#+\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()
