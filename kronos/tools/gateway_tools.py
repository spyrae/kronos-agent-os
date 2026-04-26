"""MCP Gateway management tools — agent can manage MCP servers at runtime."""

import json
import logging

from langchain_core.tools import tool

from kronos.tools.gateway import get_gateway

log = logging.getLogger("kronos.tools.gateway_tools")


@tool
def mcp_add_server(name: str, command: str, args: str = "", env_json: str = "{}") -> str:
    """Add a new MCP server to the gateway. The server will be persisted
    and available after restart. Requires reload to activate tools.

    Args:
        name: Server name (e.g. 'slack', 'jira')
        command: Command to run (e.g. 'npx', 'uvx')
        args: Space-separated arguments (e.g. '-y @slack/mcp-server')
        env_json: JSON string with environment variables (e.g. '{"SLACK_TOKEN": "xoxb-..."}')
    """
    try:
        env = json.loads(env_json) if env_json and env_json != "{}" else {}
    except json.JSONDecodeError:
        return "Error: invalid env_json format"

    config = {
        "transport": "stdio",
        "command": command,
        "args": args.split() if args else [],
    }
    if env:
        config["env"] = env

    gateway = get_gateway()
    return gateway.add_server(name, config)


@tool
def mcp_remove_server(name: str) -> str:
    """Remove a dynamic MCP server from the gateway.
    Cannot remove static servers (configured in code).

    Args:
        name: Server name to remove
    """
    gateway = get_gateway()
    return gateway.remove_server(name)


@tool
def mcp_list_servers() -> str:
    """List all registered MCP servers and their status."""
    gateway = get_gateway()
    servers = gateway.list_servers()

    if not servers:
        return "No MCP servers registered."

    lines = ["MCP Servers:"]
    for name, info in sorted(servers.items()):
        source = info["source"]
        status = "✓" if info["enabled"] else "✗"
        lines.append(f"  {status} {name} ({source})")

    return "\n".join(lines)


@tool
async def mcp_reload() -> str:
    """Reload all MCP tools from all servers. Use after adding/removing servers."""
    gateway = get_gateway()
    return await gateway.reload()


def get_gateway_tools() -> list:
    """Get all gateway management tools."""
    return [mcp_add_server, mcp_remove_server, mcp_list_servers, mcp_reload]
