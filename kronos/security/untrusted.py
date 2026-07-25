"""Marking tools whose output comes from outside the process.

Anything an agent reads from the world — a web page, an MCP server, a public
Telegram channel, a merchant name lifted out of an email — is attacker-
controllable. The engine frames such output as data before the model sees it
(``wrap_untrusted``), but only for tools that carry the marker, so the marker is
the actual security boundary.

Default posture: **external means untrusted**. A tool is trusted only when its
output is produced by this process from its own state (memory, skills, schedule).
"""

import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("kronos.security.untrusted")

UNTRUSTED_METADATA_KEY = "untrusted_output"


def tool_output_is_untrusted(tool: Any) -> bool:
    """Whether a tool returns attacker-controllable external content.

    Opt in per tool via ``metadata['untrusted_output']`` or an
    ``untrusted_output`` attribute — the same pattern as ``needs_approval``.
    """
    metadata = getattr(tool, "metadata", None) or {}
    flag = metadata.get(UNTRUSTED_METADATA_KEY)
    if flag is None:
        flag = getattr(tool, UNTRUSTED_METADATA_KEY, None)
    return bool(flag)


def mark_untrusted(tools: Iterable[Any], *, reason: str = "") -> list[Any]:
    """Mark every tool in ``tools`` as returning untrusted output.

    Returns the same objects (mutated in place) so call sites can inline it:
    ``TOOLS = mark_untrusted([...], reason="telegram channels")``.
    """
    marked = []
    for tool in tools:
        try:
            tool.metadata = {**(getattr(tool, "metadata", None) or {}), UNTRUSTED_METADATA_KEY: True}
        except (AttributeError, ValueError) as e:
            # A tool that refuses metadata cannot be protected by the engine's
            # framing, so say so loudly rather than pretending it is safe.
            log.error("Cannot mark tool %s as untrusted: %s", getattr(tool, "name", tool), e)
            continue
        marked.append(tool)

    if marked and reason:
        log.debug("Marked %d %s tool(s) as untrusted output", len(marked), reason)
    return marked
