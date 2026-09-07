"""Local approval classification for tools imported from MCP servers."""

import re
from collections.abc import Iterable

from langchain_core.tools import BaseTool

from kronos.security.untrusted import mark_untrusted

_READ_PREFIXES = ("get_", "list_", "read_", "search_", "fetch_", "retrieve_", "inspect_", "describe_")
_READ_NAMES = {"fetch", "search", "brave_web_search", "brave_local_search", "web_search_exa"}


def normalized_tool_name(name: str) -> str:
    """Normalize MCP/API name separators before applying action rules."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def mcp_requires_approval(name: str) -> bool:
    """Gate writes and unclassified MCP operations; annotations are not authority.

    Known read naming contracts remain usable without a prompt. In particular,
    HTTP POST/PATCH/PUT/DELETE tools are not exempt merely because their provider
    prefixes the name with ``API-`` or labels their output read-only.
    """
    normalized = normalized_tool_name(name)
    api_action = re.search(r"(?:^|_)api_(get|head|options|post|put|patch|delete)(?:_|$)", normalized)
    if api_action:
        return api_action.group(1) not in {"get", "head", "options"}
    return normalized not in _READ_NAMES and not normalized.startswith(_READ_PREFIXES)


def mark_mcp_tools(tools: Iterable[BaseTool], *, server: str = "") -> list[BaseTool]:
    """Attach local approval/effect policy to both startup and hot-loaded tools."""
    marked = mark_untrusted(tools, reason="mcp")
    for tool in marked:
        metadata = dict(tool.metadata or {})
        risky = mcp_requires_approval(tool.name) or bool(metadata.get("side_effect"))
        metadata.update(mcp_tool=True, needs_approval=risky, side_effect=risky)
        if server:
            metadata["mcp_server"] = server
        tool.metadata = metadata
    return marked
