"""Marking tools whose calls change something outside this process.

Durable resume replays the unanswered part of a turn. That is only safe if the
runtime can tell "this call already happened" from "this call still needs to
run" — otherwise a crash after sending a message but before journalling the
result would send it twice on recovery.

Marking is opt-in per tool, like ``needs_approval`` and ``untrusted_output``:
the runtime cannot infer intent from a function signature, and guessing wrong in
either direction is bad (a missed mark duplicates effects, a false mark silently
skips real work).
"""

import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("kronos.security.effects")

SIDE_EFFECT_METADATA_KEY = "side_effect"


def mark_side_effect(tools: Iterable[Any], *, reason: str = "") -> list[Any]:
    """Mark tools as having an external side effect. Returns the marked tools."""
    marked = []
    for tool in tools:
        try:
            tool.metadata = {**(getattr(tool, "metadata", None) or {}), SIDE_EFFECT_METADATA_KEY: True}
        except (AttributeError, ValueError) as e:
            log.error("Cannot mark tool %s as side-effecting: %s", getattr(tool, "name", tool), e)
            continue
        marked.append(tool)
    if marked and reason:
        log.debug("Marked %d %s tool(s) as side-effecting", len(marked), reason)
    return marked
