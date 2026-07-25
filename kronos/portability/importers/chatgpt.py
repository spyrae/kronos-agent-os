"""Import a ChatGPT data export (`conversations.json`).

Extracts two things: conversation threads (as session history) and explicit
memory statements the user made ("remember that…", "я предпочитаю…"). Everything
else in the export — model metadata, moderation results, node graph plumbing —
is noise for an agent bundle.

The export's `mapping` is a message tree, not a list: nodes carry `parent`
pointers and branch when a message was regenerated. We walk the parent chain
back from the last leaf, which reproduces the conversation the user actually
kept rather than every abandoned branch.
"""

import json
import logging
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from kronos.portability.build import BundleBuilder
from kronos.portability.manifest import BundleError

log = logging.getLogger("kronos.portability.importers.chatgpt")

NAME = "chatgpt"
_CONVERSATIONS = "conversations.json"

# json.load materialises the whole document; a multi-hundred-MB export would
# balloon to gigabytes of Python objects, so refuse with actionable advice
# instead of getting OOM-killed halfway through.
_MAX_JSON_BYTES = 256 * 1024 * 1024

_FACT_MARKERS = (
    "remember that",
    "remember:",
    "keep in mind",
    "i prefer",
    "my name is",
    "запомни",
    "я предпочитаю",
    "меня зовут",
    "не забывай",
)
_MAX_FACT_CHARS = 400


def detect(path: Path) -> bool:
    """True for conversations.json, a directory holding it, or an export zip."""
    if path.is_dir():
        return (path / _CONVERSATIONS).exists()
    if path.name == _CONVERSATIONS:
        return True
    if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return any(Path(name).name == _CONVERSATIONS for name in archive.namelist())
    return False


def _load_conversations(path: Path) -> list[dict]:
    """Read the conversations array from a file, directory or export zip."""
    if path.is_dir():
        return _load_conversations(path / _CONVERSATIONS)

    if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            member = next((n for n in archive.namelist() if Path(n).name == _CONVERSATIONS), None)
            if member is None:
                raise BundleError(f"{path.name} does not contain {_CONVERSATIONS}")
            info = archive.getinfo(member)
            if info.file_size > _MAX_JSON_BYTES:
                raise BundleError(_too_large_message(info.file_size))
            with archive.open(member) as handle:
                raw = json.load(handle)
    else:
        if not path.exists():
            raise BundleError(f"not found: {path}")
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise BundleError(_too_large_message(path.stat().st_size))
        raw = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(raw, dict):
        raw = raw.get("conversations", [])
    if not isinstance(raw, list):
        raise BundleError(f"{_CONVERSATIONS} does not contain a list of conversations")
    return [item for item in raw if isinstance(item, dict)]


def _too_large_message(size: int) -> str:
    return (
        f"{_CONVERSATIONS} is {size // 1024 // 1024} MB, above the {_MAX_JSON_BYTES // 1024 // 1024} MB import limit — "
        "split the export or import a subset"
    )


def _message_text(message: dict) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else None
    if isinstance(parts, list):
        chunks = [part for part in parts if isinstance(part, str)]
        return "\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    if isinstance(content, str):
        return content.strip()
    return ""


def _linear_history(conversation: dict) -> list[dict]:
    """Reconstruct the kept branch of a conversation, oldest message first."""
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return []

    # The leaf of the kept branch is the newest node that carries a message.
    def node_time(node: dict) -> float:
        message = node.get("message") or {}
        return float(message.get("create_time") or 0.0)

    candidates = [node for node in mapping.values() if isinstance(node, dict) and node.get("message")]
    if not candidates:
        return []
    node = max(candidates, key=node_time)

    chain: list[dict] = []
    seen: set[str] = set()
    while node is not None:
        message = node.get("message") or {}
        node_id = str(node.get("id") or id(node))
        if node_id in seen:
            break  # Malformed export with a parent cycle.
        seen.add(node_id)

        role = ((message.get("author") or {}).get("role") or "").strip()
        text = _message_text(message)
        if role in {"user", "assistant"} and text:
            chain.append({"type": "human" if role == "user" else "ai", "content": text})

        parent_id = node.get("parent")
        node = mapping.get(parent_id) if isinstance(parent_id, str) else None

    chain.reverse()
    return chain


def _iso(timestamp: object) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp or 0), tz=UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _harvest_facts(builder: BundleBuilder, text: str, *, user_id: str, created_at: str) -> int:
    """Pull explicit memory statements out of one user message."""
    added = 0
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        candidate = sentence.strip()
        if not candidate or len(candidate) > _MAX_FACT_CHARS:
            continue
        lowered = candidate.lower()
        if any(marker in lowered for marker in _FACT_MARKERS):
            if builder.add_fact(candidate, user_id=user_id, created_at=created_at, source="chatgpt"):
                added += 1
    return added


def to_bundle(
    path: str | Path,
    out_path: str | Path,
    *,
    limit: int | None = None,
    user_id: str = "",
    created_at: str = "",
):
    """Convert a ChatGPT export into a bundle at ``out_path``."""
    from kronos.config import settings
    from kronos.portability.importers import ImporterResult

    source = Path(path)
    conversations = _load_conversations(source)
    conversations.sort(key=lambda item: float(item.get("update_time") or item.get("create_time") or 0.0), reverse=True)

    dropped = 0
    if limit is not None and len(conversations) > limit:
        dropped = len(conversations) - limit
        conversations = conversations[:limit]

    builder = BundleBuilder(agent_name=NAME, created_at=created_at)
    owner = user_id or settings.agent_name
    threads = 0

    for index, conversation in enumerate(conversations):
        history = _linear_history(conversation)
        if not history:
            continue
        title = str(conversation.get("title") or f"conversation-{index + 1}").strip()
        updated = _iso(conversation.get("update_time") or conversation.get("create_time"))
        builder.add_session(f"chatgpt:{index + 1}:{title[:40]}", history, updated_at=updated)
        threads += 1
        for message in history:
            if message["type"] == "human":
                _harvest_facts(builder, message["content"], user_id=owner, created_at=updated)

    if builder.is_empty():
        raise BundleError("ChatGPT export produced nothing importable (no conversations with messages)")

    if dropped:
        builder.warnings.append(f"limited to {limit} most recent conversations, {dropped} older ones skipped")

    bundle, manifest = builder.write(out_path)
    log.info("ChatGPT import: %d threads, %d facts", threads, len(builder.facts))
    return ImporterResult(
        importer=NAME,
        bundle=bundle,
        manifest=manifest,
        counts=builder.counts(),
        warnings=builder.warnings,
    )
