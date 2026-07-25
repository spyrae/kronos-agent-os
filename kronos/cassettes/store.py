"""Cassette storage: content-addressed request/response records.

A cassette lets the same agent turn run twice and produce the same result
without calling a provider — the basis for deterministic evals in CI.

Two rules make the key stable:

* only what the model actually sees enters the key (role, content, tool
  name/args, tool descriptions) — never a generated id, timestamp or usage count;
* the provider's model name is stored but NOT keyed: replay runs with no
  providers configured cannot know which model would have answered, and a key
  they cannot reproduce is a key that never hits. Comparing two models is a
  separate run with its own KAOS_CASSETTE_DIR;
* the key is computed over the **redacted** form, so a cassette recorded from a
  real conversation matches a scenario replayed from a scrubbed copy, and
  nothing secret is what identifies a record.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from kronos.portability.redact import redact_structure

log = logging.getLogger("kronos.cassettes.store")

KIND_LLM = "llm"
KIND_TOOL = "tools"


def _normalize_content(content: Any) -> Any:
    """Reduce message content to a comparable shape."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_normalize_content(block) for block in content]
    if isinstance(content, dict):
        return {str(key): _normalize_content(value) for key, value in sorted(content.items())}
    return content


def _normalize_message(message: BaseMessage) -> dict:
    """Keep role, content and tool intent; drop ids and provider metadata.

    Tool-call ids are random per run, so including them would make every key
    unique and no cassette would ever hit.
    """
    row: dict[str, Any] = {
        "type": getattr(message, "type", message.__class__.__name__.lower()),
        "content": _normalize_content(message.content),
    }
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        row["tool_calls"] = [
            {"name": call.get("name", ""), "args": _normalize_content(call.get("args", {}))} for call in tool_calls
        ]
    return row


def _normalize_tools(tools: list | None) -> list[dict]:
    """Tool name plus description — both steer the model, so both key the call."""
    rows = []
    for tool in tools or []:
        name = getattr(tool, "name", None) or str(tool)
        description = (getattr(tool, "description", "") or "").strip()
        rows.append({"name": name, "description": description})
    return sorted(rows, key=lambda row: row["name"])


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def llm_key(*, messages: list[BaseMessage], tools: list | None, label: str) -> str:
    """Content key for one model call (label + conversation + tool surface)."""
    payload = {
        "label": label,
        "messages": [_normalize_message(message) for message in messages],
        "tools": _normalize_tools(tools),
    }
    return _digest(redact_structure(payload, mask_personal=True))


def tool_key(*, tool_name: str, args: Any) -> str:
    """Content key for one tool call."""
    payload = {"tool": tool_name, "args": _normalize_content(args)}
    return _digest(redact_structure(payload, mask_personal=True))


def serialize_ai_message(message: AIMessage) -> dict:
    """Store an assistant response, keeping tool-call ids so a replayed turn wires up."""
    return {
        "content": message.content,
        "tool_calls": [
            {"name": call.get("name", ""), "args": call.get("args", {}), "id": call.get("id", "")}
            for call in (message.tool_calls or [])
        ],
        "additional_kwargs": {
            key: value for key, value in (message.additional_kwargs or {}).items() if key != "function_call"
        },
        "response_metadata": {"cassette": True},
    }


def deserialize_ai_message(payload: dict) -> AIMessage:
    """Rebuild the assistant response recorded in a cassette."""
    tool_calls = [
        {"name": call.get("name", ""), "args": call.get("args", {}), "id": call.get("id", ""), "type": "tool_call"}
        for call in payload.get("tool_calls") or []
    ]
    return AIMessage(
        content=payload.get("content", ""),
        tool_calls=tool_calls,
        additional_kwargs=payload.get("additional_kwargs") or {},
        response_metadata=payload.get("response_metadata") or {"cassette": True},
    )


class CassetteStore:
    """Reads and writes cassettes under a directory tree."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, kind: str, key: str, *, group: str = "") -> Path:
        """Shard by key prefix (or tool name) to keep directories browsable."""
        bucket = group or key[:2]
        return self.root / kind / bucket / f"{key}.json"

    def read(self, kind: str, key: str, *, group: str = "") -> dict | None:
        path = self.path_for(kind, key, group=group)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.warning("Ignoring malformed cassette %s: %s", path, e)
            return None

    def write(self, kind: str, key: str, payload: dict, *, group: str = "") -> Path:
        """Persist a cassette. Content is redacted — cassettes live in git."""
        path = self.path_for(kind, key, group=group)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"key": key, **redact_structure(payload, mask_personal=True)}
        path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return path

    def count(self, kind: str = "") -> int:
        base = self.root / kind if kind else self.root
        if not base.exists():
            return 0
        return sum(1 for path in base.rglob("*.json") if path.is_file())
