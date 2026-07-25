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

from kronos.config import settings
from kronos.security.sanitize import detect_injection, strip_injection, wrap_untrusted

log = logging.getLogger("kronos.security.untrusted")

UNTRUSTED_METADATA_KEY = "untrusted_output"

INJECTION_ACTION_LOG = "log"
INJECTION_ACTION_STRIP = "strip"
INJECTION_ACTION_BLOCK = "block"
INJECTION_BLOCKED_MESSAGE = "[BLOCKED] injection attempt detected in external content"


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


def handle_injection(content: str, *, source: str) -> tuple[str, list[str]]:
    """Detect and react to injection attempts in externally-sourced text.

    Shared by the tool path (``engine.execute_tool``) and the cron pipelines
    (signal digests, competitor diffs), because "content from the world reached a
    prompt" is the same risk whether a model asked for it or a schedule did.

    Framing already tells the model to treat this as data, so the default is to
    record the attempt rather than alter the payload — an attempt is signal, and
    silently rewriting external content makes debugging harder. Deployments that
    prefer defence over fidelity set ``strip`` or ``block``.

    Returns (content, matches); empty matches means nothing was found.
    """
    matches = detect_injection(content)
    if not matches:
        return content, []

    action = (settings.untrusted_injection_action or INJECTION_ACTION_LOG).strip().lower()
    log.warning(
        "Injection attempt in external content from %s (action=%s): %s",
        source,
        action,
        "; ".join(matches[:3])[:200],
    )
    _record_injection(source, matches)

    if action == INJECTION_ACTION_BLOCK:
        return INJECTION_BLOCKED_MESSAGE, matches
    if action == INJECTION_ACTION_STRIP:
        cleaned, _ = strip_injection(content)
        return cleaned, matches
    return content, matches


def frame_external(content: str, *, source: str) -> str:
    """Prepare externally-sourced text for a prompt: react, then frame as data.

    Use this wherever content the agent did not author is interpolated into a
    prompt outside the tool loop. Frame the *fragment*, not the whole prompt —
    wrapping your own instructions as untrusted data teaches the model to ignore
    them.
    """
    if not content:
        return content
    handled, _ = handle_injection(content, source=source)
    return wrap_untrusted(handled, label=source)


def _record_injection(source: str, matches: list[str]) -> None:
    """Audit + count the attempt. Observability must never break the caller."""
    try:
        from kronos.audit import log_tool_event
        from kronos.swarm_store import get_swarm

        log_tool_event(
            "tool_injection",
            {"name": source, "content": f"patterns: {'; '.join(matches[:5])}", "capability": "security"},
        )
        get_swarm().incr_metric("injections_detected")
    except Exception as e:
        log.debug("Could not record injection event: %s", e)
