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
        "args": ["mcp-server-fetch"],
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
                    '{"Authorization":"Bearer ' + settings.notion_api_key + '",'
                    '"Notion-Version":"2022-06-28"}'
                ),
            },
        }
    else:
        log.debug("Skipping notion: NOTION_API_KEY not set")

    if settings.google_oauth_client_id and settings.google_oauth_client_secret:
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
        "args": ["--from", "mcp-yahoo-finance", "mcp-yahoo-finance"],
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
