"""MCP tool manager — lifecycle management for MCP servers.

Handles startup, tool loading, and graceful shutdown of all MCP servers
through langchain-mcp-adapters MultiServerMCPClient.

Resilient loading: each server is tried independently so one failure
doesn't prevent the rest from starting.
"""

import logging
from contextlib import asynccontextmanager

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from kronos.tools.mcp_servers import build_mcp_config

log = logging.getLogger("kronos.tools.manager")


async def _load_server_tools(name: str, server_config: dict) -> list[BaseTool]:
    """Load tools from a single MCP server, returning [] on failure.

    Each tool gets a `mcp_server` attribute set to the server name,
    so sub-agents can filter tools by origin server.
    """
    try:
        client = MultiServerMCPClient({name: server_config})
        tools = await client.get_tools()
        for tool in tools:
            tool.metadata = {**(tool.metadata or {}), "mcp_server": name}
        log.info("  [%s] loaded %d tools", name, len(tools))
        return tools
    except Exception:
        log.exception("  [%s] FAILED to load — skipping", name)
        return []


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
        tools = await _load_server_tools(name, server_config)
        if tools:
            all_tools.extend(tools)
        else:
            failed.append(name)

    if failed:
        log.warning("Failed servers (%d/%d): %s", len(failed), len(config), failed)

    log.info(
        "Loaded %d tools from %d/%d servers",
        len(all_tools), len(config) - len(failed), len(config),
    )

    for tool in all_tools:
        log.debug("  Tool: %s", tool.name)

    yield all_tools

    log.info("MCP tools session ended")
