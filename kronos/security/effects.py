"""Marking tools whose calls change something outside this process.

Durable resume replays the unanswered part of a turn. That is only safe if the
runtime can tell "this call already happened" from "this call still needs to
run" — otherwise a crash after sending a message but before journalling the
result would send it twice on recovery.

Explicit metadata is preferred. The engine also recognizes legacy mutation names;
MCP tools receive local classification at import. Neither a function signature
nor the presence of an approval prompt alone proves that an operation is safe.
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


def mark_read_only(tools: Iterable[Any], *, reason: str = "") -> list[Any]:
    """Declare that calling these tools changes nothing outside this process.

    An explicit ``False`` is a statement, not a default: it is what lets an
    interrupted turn run the call again without review. A tool that reads
    through an authenticated session still counts as read-only if it only reads.
    """
    marked = []
    for tool in tools:
        try:
            tool.metadata = {**(getattr(tool, "metadata", None) or {}), SIDE_EFFECT_METADATA_KEY: False}
        except (AttributeError, ValueError) as e:
            # Undeclared is the safe failure: resume then refuses to replay it.
            log.error("Cannot mark tool %s as read-only: %s", getattr(tool, "name", tool), e)
            continue
        marked.append(tool)
    if marked and reason:
        log.debug("Marked %d %s tool(s) as read-only", len(marked), reason)
    return marked
