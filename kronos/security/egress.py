"""Policy-driven egress control: where the agent may reach.

This sits *next to* `tools/browser/security.is_url_safe`, which blocks dangerous
schemes and private-network addresses (SSRF). That check is about what must never
be reachable; this one is about what a given deployment chose to allow. Both run:
an allowlisted host is still refused if it resolves to a private range.

Rollout is designed to be survivable: `mode: allowlist` with `dry_run: true`
only logs what *would* be blocked, so an operator can watch a day of real traffic
before enforcing and discovering that a cron job needed a domain nobody listed.
"""

import ipaddress
import logging
from urllib.parse import urlparse

log = logging.getLogger("kronos.security.egress")

# Demo mode forces allowlist regardless of the policy file, mirroring how
# `_force_demo_safety` clamps capability gates.
_forced_allowlist = False


class EgressBlockedError(Exception):
    """Raised when a destination is not permitted by policy."""


def force_allowlist(enabled: bool = True) -> None:
    """Force allowlist mode process-wide (demo mode)."""
    global _forced_allowlist
    _forced_allowlist = enabled


def _egress_policy():
    from kronos.policy import get_policy

    return get_policy().egress


def host_allowed(host: str, domains: list[str]) -> bool:
    """Match a hostname against allowlist entries.

    Supports an exact host and a single-level wildcard (`*.example.com`, which
    also matches `example.com`). No regex: an allowlist people cannot read by
    glancing at it is an allowlist that gets bypassed.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for entry in domains:
        pattern = (entry or "").strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if host == suffix or host.endswith(f".{suffix}"):
                return True
        elif host == pattern:
            return True
    return False


def _is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def check_url(url: str, *, tool: str = "") -> None:
    """Raise EgressBlockedError when policy forbids reaching ``url``.

    Localhost stays reachable: a local Ollama, LiteLLM proxy or dashboard is not
    egress in any meaningful sense, and blocking it would break local-first
    deployments for no security gain (SSRF is handled by the browser check).
    """
    policy = _egress_policy()
    mode = "allowlist" if _forced_allowlist else policy.mode
    if mode != "allowlist":
        return

    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise EgressBlockedError(f"cannot determine host for {url!r}")
    if host in {"localhost", "127.0.0.1", "::1"} or _is_private(host):
        return
    if host_allowed(host, policy.domains):
        return

    detail = f"egress to {host} is not in the allowlist"
    if policy.dry_run and not _forced_allowlist:
        # Observation phase: report what enforcement would have stopped.
        log.warning("Egress (dry-run) would block %s%s", host, f" for {tool}" if tool else "")
        _record_block(host, tool, enforced=False)
        return

    log.warning("Egress blocked: %s%s", host, f" for {tool}" if tool else "")
    _record_block(host, tool, enforced=True)
    raise EgressBlockedError(detail)


def check_command(command: str, *, server: str = "") -> None:
    """Raise when a stdio MCP server's command is not permitted.

    An MCP server is a local process, so its allowlist is a command list rather
    than a domain list. An empty `allowed_commands` means unrestricted — a
    deployment that wants command control must say which commands it wants.
    """
    policy = _egress_policy()
    mode = "allowlist" if _forced_allowlist else policy.mode
    if mode != "allowlist" or not policy.allowed_commands:
        return

    binary = (command or "").strip().split("/")[-1]
    if binary in {entry.strip().split("/")[-1] for entry in policy.allowed_commands}:
        return

    detail = f"MCP command {binary!r} is not in allowed_commands"
    if policy.dry_run and not _forced_allowlist:
        log.warning("Egress (dry-run) would block MCP command %s (server=%s)", binary, server or "?")
        _record_block(f"cmd:{binary}", server, enforced=False)
        return

    log.warning("Blocked MCP command %s (server=%s)", binary, server or "?")
    _record_block(f"cmd:{binary}", server, enforced=True)
    raise EgressBlockedError(detail)


def _record_block(target: str, source: str, *, enforced: bool) -> None:
    """Log the decision to the audit trail and count it."""
    try:
        from kronos.audit import log_tool_event
        from kronos.swarm_store import get_swarm

        log_tool_event(
            "egress_blocked" if enforced else "egress_dry_run",
            {"name": source or "egress", "content": target, "capability": "security", "ok": False},
        )
        get_swarm().incr_metric("egress_blocked" if enforced else "egress_dry_run")
    except Exception as e:  # observability must never break the caller
        log.debug("Could not record egress decision: %s", e)
