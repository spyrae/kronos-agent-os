"""MCP tool manager — lifecycle management for MCP servers.

Handles startup, tool loading, and graceful shutdown of all MCP servers
through langchain-mcp-adapters MultiServerMCPClient.

Resilient loading: each server is tried independently so one failure
doesn't prevent the rest from starting.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from kronos.audit import redact_secrets
from kronos.health import STATUS_BROKEN, STATUS_OFF, STATUS_OK, HealthCheck
from kronos.tools.mcp_servers import KNOWN_SERVERS, build_mcp_config

log = logging.getLogger("kronos.tools.manager")

# One server at a time, and none allowed to hang the probe. Startup takes about
# forty seconds for eleven servers; a daily job can afford that, and matching the
# way startup actually loads them is worth more than finishing sooner.
PROBE_TIMEOUT_SECONDS = 60


async def _load_server_tools(
    name: str,
    server_config: dict,
    timeout: float | None = None,
) -> tuple[list[BaseTool], str]:
    """Load tools from one MCP server. Returns (tools, why-it-failed).

    Each tool gets a `mcp_server` attribute set to the server name,
    so sub-agents can filter tools by origin server.

    The reason comes back as well as being logged because the health check needs
    to *report* it, and a traceback in the journal is exactly the form in which
    two dead servers went unnoticed for months.

    ``timeout`` is for that health check, not for startup: bounding startup would
    change when a slow-but-working server is given up on, and that is a different
    decision from bounding a daily probe.
    """
    try:
        client = MultiServerMCPClient({name: server_config})
        tools = await (asyncio.wait_for(client.get_tools(), timeout) if timeout else client.get_tools())
        for tool in tools:
            tool.metadata = {**(tool.metadata or {}), "mcp_server": name}
        log.info("  [%s] loaded %d tools", name, len(tools))
        return tools, ""
    except TimeoutError:
        log.error("  [%s] did not answer within %ss — skipping", name, timeout)
        return [], f"no answer within {timeout}s"
    except Exception as e:
        log.exception("  [%s] FAILED to load — skipping", name)
        return [], f"{type(e).__name__}: {e}"


@asynccontextmanager
async def managed_mcp_tools():
    """Context manager that loads MCP tools and yields them.

    Each server is loaded independently — a failing server is skipped
    and the rest continue to work.

    Usage:
        async with managed_mcp_tools() as tools:
            graph = build_graph(tools=tools)
            ...
    """
    config = build_mcp_config()

    if not config:
        log.warning("No MCP servers configured, running without tools")
        yield []
        return

    log.info("Starting %d MCP servers...", len(config))

    all_tools: list[BaseTool] = []
    failed = []

    for name, server_config in config.items():
        tools, _error = await _load_server_tools(name, server_config)
        if tools:
            all_tools.extend(tools)
        else:
            failed.append(name)

    if failed:
        log.warning("Failed servers (%d/%d): %s", len(failed), len(config), failed)

    log.info(
        "Loaded %d tools from %d/%d servers",
        len(all_tools),
        len(config) - len(failed),
        len(config),
    )

    for tool in all_tools:
        log.debug("  Tool: %s", tool.name)

    yield all_tools

    log.info("MCP tools session ended")


# Long enough to be a key rather than a word. Used to find credentials embedded
# inside a larger env value — the Notion header carries its token inside a JSON
# string, so redacting the whole value would miss the token quoted on its own.
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-]{16,}")


def _without_credentials(text: str, server_config: dict) -> str:
    """Strip a server's own credentials out of text that is about to be reported.

    The detail here is exception text, this report goes to Telegram, and MCP
    server configs carry API keys in `env`. A library that helpfully includes the
    config it was handed would put a live key into a chat message — so redaction
    is by *value*, since this is the one place that holds the values, with the
    pattern-based pass behind it for anything shaped like a token.

    Over-redacting an error message costs nothing; under-redacting it once is
    permanent.
    """
    secrets: list[str] = []
    for raw in (server_config.get("env") or {}).values():
        value = str(raw or "")
        if len(value) >= 8:
            secrets.append(value)
        secrets.extend(_TOKEN_LIKE.findall(value))

    # Longest first, so a value containing another is replaced whole.
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return redact_secrets(text)


async def check_mcp_health() -> list[HealthCheck]:
    """Start every known MCP server and report which ones hand over tools.

    This exists because two servers were dead for months and nothing said so.
    The loading here is deliberately resilient — a server that will not start is
    skipped so the rest keep working — which is right, and which also means the
    only trace of a failure is a traceback in a journal nobody reads. The agents
    came up with 102 tools instead of 113 and looked perfectly healthy.

    A server absent from the built config reports `off` rather than being left
    out: a key dropped from `.env` makes a server *vanish*, and something that
    silently ceases to exist is precisely what needs saying. `off` is not a fault
    — most deployments configure a handful of these — but going from working to
    absent is.
    """
    config = build_mcp_config()
    checks: list[HealthCheck] = []

    for name in KNOWN_SERVERS:
        if name not in config:
            checks.append(HealthCheck(name, STATUS_OFF, "not configured here (no credentials, or not this agent's)"))
            continue

        tools, error = await _load_server_tools(name, config[name], timeout=PROBE_TIMEOUT_SECONDS)
        if error:
            checks.append(HealthCheck(name, STATUS_BROKEN, _without_credentials(error, config[name])[:300]))
        elif not tools:
            # Distinct from an error on purpose: a server that starts cleanly and
            # offers nothing is contributing nothing, and reads as healthy to
            # every check that stops at "did the process come up".
            checks.append(HealthCheck(name, STATUS_BROKEN, "started but exposed no tools"))
        else:
            checks.append(HealthCheck(name, STATUS_OK, f"{len(tools)} tools"))

    return checks
