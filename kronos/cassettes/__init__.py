"""Deterministic record/replay for provider and tool calls.

Why this exists: an agent's behaviour is worth checking on every change, but a
suite that calls real providers is neither free nor repeatable. Cassettes let a
real turn be recorded once and replayed forever — no keys, no network, same
answer.

Modes, from ``KAOS_CASSETTE_MODE``:

* ``off`` (default) — completely transparent, production path untouched;
* ``record`` — call the provider, store the response;
* ``replay`` — never call a provider; a missing cassette is an error.

The mode is read per call rather than cached, so a scenario runner can switch it
mid-process without rebuilding the model factory.
"""

import logging
import os
from pathlib import Path
from typing import Any

from kronos.cassettes.model import CassetteChatModel, CassetteMissError
from kronos.cassettes.store import (
    KIND_LLM,
    KIND_TOOL,
    CassetteStore,
    deserialize_ai_message,
    llm_key,
    serialize_ai_message,
    tool_key,
)

log = logging.getLogger("kronos.cassettes")

MODE_OFF = "off"
MODE_RECORD = "record"
MODE_REPLAY = "replay"
MODES = (MODE_OFF, MODE_RECORD, MODE_REPLAY)

ENV_MODE = "KAOS_CASSETTE_MODE"
ENV_DIR = "KAOS_CASSETTE_DIR"
DEFAULT_DIR = "./data/cassettes"


def mode() -> str:
    """Current cassette mode; anything unrecognised is treated as off."""
    raw = (os.environ.get(ENV_MODE) or MODE_OFF).strip().lower()
    if raw not in MODES:
        log.warning("Unknown %s=%r, falling back to 'off'", ENV_MODE, raw)
        return MODE_OFF
    return raw


def active() -> bool:
    return mode() != MODE_OFF


def replaying() -> bool:
    return mode() == MODE_REPLAY


def recording() -> bool:
    return mode() == MODE_RECORD


def cassette_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def get_store() -> CassetteStore:
    return CassetteStore(cassette_dir())


def wrap_model(inner: Any, *, label: str) -> Any:
    """Wrap a live model for recording; return it untouched when mode is off."""
    current = mode()
    if current == MODE_OFF:
        return inner
    return CassetteChatModel(inner, label=label, mode=current, store=get_store())


def replay_model(*, label: str) -> CassetteChatModel:
    """A model that only replays — constructed without any provider or key."""
    return CassetteChatModel(None, label=label, mode=MODE_REPLAY, store=get_store())


def read_tool_result(tool_name: str, args: Any) -> str | None:
    """Recorded output for a tool call, or None when there is no cassette."""
    payload = get_store().read(KIND_TOOL, tool_key(tool_name=tool_name, args=args), group=_tool_group(tool_name))
    if payload is None:
        return None
    content = payload.get("content")
    return content if isinstance(content, str) else None


def write_tool_result(tool_name: str, args: Any, content: str) -> None:
    """Record a tool result so replay never has to reach the outside world."""
    get_store().write(
        KIND_TOOL,
        tool_key(tool_name=tool_name, args=args),
        {"tool": tool_name, "content": content},
        group=_tool_group(tool_name),
    )


def _tool_group(tool_name: str) -> str:
    """Directory-safe tool name, so cassettes stay readable per tool."""
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in tool_name)
    return safe or "unknown"


__all__ = [
    "DEFAULT_DIR",
    "ENV_DIR",
    "ENV_MODE",
    "KIND_LLM",
    "KIND_TOOL",
    "MODES",
    "MODE_OFF",
    "MODE_RECORD",
    "MODE_REPLAY",
    "CassetteChatModel",
    "CassetteMissError",
    "CassetteStore",
    "active",
    "cassette_dir",
    "deserialize_ai_message",
    "get_store",
    "llm_key",
    "mode",
    "read_tool_result",
    "recording",
    "replay_model",
    "replaying",
    "serialize_ai_message",
    "tool_key",
    "wrap_model",
    "write_tool_result",
]
