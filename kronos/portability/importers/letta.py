"""Import a Letta/MemGPT agent file (`.af`).

Letta stores what KAOS splits across persona and memory in labelled blocks:

* the ``persona`` block is self-description → IDENTITY.md;
* the ``system`` prompt is working instruction → methodology.md;
* the ``human`` block is what the agent knows about its owner → facts, split per
  line, because that block is conventionally a list of statements;
* messages become one session thread.

Blocks with other labels are kept as notes rather than dropped — a custom block
is usually the most agent-specific content in the file.
"""

import json
import logging
from pathlib import Path

from kronos.portability.build import BundleBuilder
from kronos.portability.manifest import BundleError

log = logging.getLogger("kronos.portability.importers.letta")

NAME = "letta"

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_FACT_CHARS = 400
_ROLE_MAP = {"user": "human", "assistant": "ai", "system": "system", "tool": "tool"}


def _looks_like_agent_file(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get("agent_type")
        or payload.get("memory_blocks")
        or payload.get("core_memory")
        or (payload.get("name") and payload.get("system"))
    )


def detect(path: Path) -> bool:
    """True for a `.af` file, or a JSON file shaped like an agent file."""
    if not path.is_file():
        return False
    if path.suffix.lower() not in {".af", ".json"}:
        return False
    if path.stat().st_size > _MAX_FILE_BYTES:
        return False
    try:
        return _looks_like_agent_file(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return False


def _blocks(payload: dict) -> list[dict]:
    raw = payload.get("memory_blocks") or payload.get("core_memory") or []
    if isinstance(raw, dict):
        # Older dumps used {"persona": "...", "human": "..."}.
        return [{"label": key, "value": value} for key, value in raw.items()]
    return [block for block in raw if isinstance(block, dict)]


def _message_text(message: dict) -> str:
    for key in ("text", "content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = [part.get("text", "") if isinstance(part, dict) else str(part) for part in value]
            joined = "\n".join(part for part in parts if part).strip()
            if joined:
                return joined
    return ""


def to_bundle(
    path: str | Path,
    out_path: str | Path,
    *,
    limit: int | None = None,
    user_id: str = "",
    created_at: str = "",
):
    """Convert a Letta agent file into a bundle at ``out_path``."""
    from kronos.config import settings
    from kronos.portability.importers import ImporterResult

    source = Path(path)
    if not source.is_file():
        raise BundleError(f"not an agent file: {source}")
    if source.stat().st_size > _MAX_FILE_BYTES:
        raise BundleError(f"{source.name} is above the {_MAX_FILE_BYTES // 1024 // 1024} MB import limit")

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BundleError(f"{source.name} is not valid JSON: {e}") from e
    if not _looks_like_agent_file(payload):
        raise BundleError(f"{source.name} does not look like a Letta agent file")

    builder = BundleBuilder(agent_name=NAME, created_at=created_at)
    owner = user_id or settings.agent_name
    agent_name = str(payload.get("name") or "letta-agent").strip()

    system = str(payload.get("system") or "").strip()
    if system:
        builder.add_persona("methodology.md", f"# Imported system prompt ({agent_name})\n\n{system}\n")

    for block in _blocks(payload):
        label = str(block.get("label") or "").strip().lower()
        value = str(block.get("value") or "").strip()
        if not value:
            continue
        if label == "persona":
            builder.add_persona("IDENTITY.md", f"# {agent_name}\n\n{value}\n")
        elif label == "human":
            for line in value.splitlines():
                fact = line.strip().lstrip("-*• ").strip()
                if fact:
                    builder.add_fact(fact[:_MAX_FACT_CHARS], user_id=owner, source="letta")
        else:
            builder.add_note(f"world/letta/{label or 'block'}.md", f"# {label}\n\n{value}\n")

    messages = payload.get("messages")
    history: list[dict] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("message_type") or "").lower()
            text = _message_text(message)
            if not text or role not in _ROLE_MAP:
                continue
            history.append({"type": _ROLE_MAP[role], "content": text})
        if limit is not None and len(history) > limit:
            dropped = len(history) - limit
            history = history[-limit:]
            builder.warnings.append(f"limited to the last {limit} messages, {dropped} older ones skipped")
    if history:
        builder.add_session(f"letta:{agent_name}", history)

    if builder.is_empty():
        raise BundleError(f"agent file {source.name} produced nothing importable")

    bundle, manifest = builder.write(out_path)
    log.info("Letta import: %s", builder.counts())
    return ImporterResult(
        importer=NAME,
        bundle=bundle,
        manifest=manifest,
        counts=builder.counts(),
        warnings=builder.warnings,
    )
