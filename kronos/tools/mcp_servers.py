"""MCP server configuration — migrated from Kronos I mcporter.json.

Each server runs via stdio transport through langchain-mcp-adapters.
Servers are started lazily and managed by MultiServerMCPClient.
"""

import logging
import os
import shutil

from kronos.config import settings

log = logging.getLogger("kronos.tools.mcp")


def _find_uvx() -> str:
    """Find uvx binary path (varies by OS)."""
    # Common locations
    for path in [
        shutil.which("uvx"),
        os.path.expanduser("~/.local/bin/uvx"),
        "/home/node/.local/bin/uvx",
    ]:
        if path and os.path.isfile(path):
            return path
    return "uvx"  # hope it's on PATH


# Third-party servers written against MCP SDK 1.x, which declare `mcp>=1.6` with
# no upper bound. uv honours that literally and installs 2.0, where the API each
# of them uses is gone — `mcp-server-fetch` dies importing `McpError`,
# `mcp-yahoo-finance` on `Server.list_tools`. Both had been failing on every boot
# for months, costing 11 tools (the whole of the finance agent's market data) and
# leaving a traceback in the journal that trained everyone to skip startup errors.
#
# The pin lives inside each server's own ephemeral uvx environment, so it is
# local to them: this process keeps whatever SDK it wants, and the two halves
# talk over the wire protocol, which negotiates versions. Verified by loading
# their tools through this app's own client, not by watching them start.
#
# Neither package has shipped since 2025, so there is no forward fix to wait for.
# Drop the pin only after confirming the upstream release actually supports 2.x.
SDK_1X = ["--with", "mcp<2"]


def build_mcp_config() -> dict:
    """Build MultiServerMCPClient configuration dict.

    Returns config suitable for MultiServerMCPClient({...}).
    Only includes servers whose required env vars are available.
    """
    uvx = _find_uvx()
    workspace_path = os.path.abspath(settings.workspace_path)

    servers = {}

    # --- Search & Web ---

    if settings.brave_api_key:
        servers["brave-search"] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@brave/brave-search-mcp-server"],
            "env": {"BRAVE_API_KEY": settings.brave_api_key},
        }
    else:
        log.debug("Skipping brave-search: BRAVE_API_KEY not set")

    if settings.exa_api_key:
        servers["exa"] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "exa-mcp-server"],
            "env": {"EXA_API_KEY": settings.exa_api_key},
        }
    else:
        log.debug("Skipping exa: EXA_API_KEY not set")

    servers["fetch"] = {
        "transport": "stdio",
        "command": uvx,
        "args": [*SDK_1X, "mcp-server-fetch"],
    }

    servers["content-core"] = {
        "transport": "stdio",
        "command": uvx,
        "args": ["--from", "content-core", "content-core-mcp"],
    }

    servers["reddit"] = {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "reddit-mcp-buddy"],
    }

    # --- Productivity ---

    if settings.notion_api_key:
        servers["notion"] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {
                "OPENAPI_MCP_HEADERS": (
                    '{"Authorization":"Bearer ' + settings.notion_api_key + '","Notion-Version":"2022-06-28"}'
                ),
            },
        }
    else:
        log.debug("Skipping notion: NOTION_API_KEY not set")

    google_workspace_agent = os.environ.get("GOOGLE_WORKSPACE_MCP_AGENT", "kronos").strip().lower()
    if settings.agent_name.lower() != google_workspace_agent:
        log.info(
            "Skipping google-workspace: enabled only for AGENT_NAME=%s to avoid OAuth helper port collisions",
            google_workspace_agent,
        )
    elif settings.google_oauth_client_id and settings.google_oauth_client_secret:
        servers["google-workspace"] = {
            "transport": "stdio",
            "command": uvx,
            "args": ["workspace-mcp", "--tool-tier", "core"],
            "env": {
                "GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
                "GOOGLE_OAUTH_CLIENT_SECRET": settings.google_oauth_client_secret,
            },
        }
    else:
        log.debug("Skipping google-workspace: OAuth credentials not set")

    # --- Media ---

    servers["youtube"] = {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@kimtaeyoon83/mcp-server-youtube-transcript"],
    }

    servers["markitdown"] = {
        "transport": "stdio",
        "command": uvx,
        "args": ["markitdown-mcp"],
    }

    # --- Finance ---

    servers["yahoo-finance"] = {
        "transport": "stdio",
        "command": uvx,
        # --with must precede the command uvx is asked to run, or uv reads it as
        # an argument to that command instead of a dependency of the environment.
        "args": ["--from", "mcp-yahoo-finance", *SDK_1X, "mcp-yahoo-finance"],
    }

    # --- Filesystem ---

    if os.path.isdir(workspace_path):
        servers["filesystem"] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", workspace_path],
        }
    else:
        log.warning("Skipping filesystem: workspace path %s not found", workspace_path)

    log.info("MCP servers configured: %s", list(servers.keys()))
    return servers
