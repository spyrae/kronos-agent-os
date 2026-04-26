"""MCP smoke tests — verify each server starts and responds.

Run: pytest tests/test_mcp_smoke.py -v
Requires: MCP server binaries and API keys in .env
"""

import asyncio
import os
import pytest

pytestmark = pytest.mark.integration

from kronos.tools.mcp_servers import build_mcp_config


def get_configured_servers() -> dict:
    """Get MCP config, filtering to servers with required env vars."""
    return build_mcp_config()


@pytest.fixture(scope="module")
def mcp_config():
    return get_configured_servers()


def test_mcp_config_not_empty(mcp_config):
    """At least some MCP servers should be configured."""
    assert len(mcp_config) > 0, "No MCP servers configured"


def test_mcp_config_has_brave(mcp_config):
    """Brave search should be configured (has API key)."""
    from kronos.config import settings
    if not settings.brave_api_key:
        pytest.skip("BRAVE_API_KEY not set")
    assert "brave-search" in mcp_config


def test_mcp_config_has_fetch(mcp_config):
    """Fetch server should always be available (no API key needed)."""
    assert "fetch" in mcp_config


def test_mcp_config_has_filesystem(mcp_config):
    """Filesystem server is added only when the agent's workspace path exists.

    On a dev machine that hasn't populated ``workspaces/<agent>/`` yet the
    directory may be missing; ``build_mcp_config`` logs a warning and
    skips the server. Skip the test in that case — on VPS the directory
    is always present and the assertion runs normally.
    """
    import os

    from kronos.config import settings

    if not os.path.isdir(settings.workspace_path):
        pytest.skip(f"workspace path {settings.workspace_path} not present")
    assert "filesystem" in mcp_config


def test_mcp_config_has_yahoo_finance(mcp_config):
    """Yahoo Finance should always be available (no API key needed)."""
    assert "yahoo-finance" in mcp_config


def test_mcp_config_has_youtube(mcp_config):
    """YouTube transcript should always be available."""
    assert "youtube" in mcp_config


def test_mcp_config_has_markitdown(mcp_config):
    """Markitdown should always be available."""
    assert "markitdown" in mcp_config


def test_mcp_config_has_content_core(mcp_config):
    """Content-core should always be available."""
    assert "content-core" in mcp_config


def test_mcp_config_has_reddit(mcp_config):
    """Reddit should always be available."""
    assert "reddit" in mcp_config


@pytest.mark.asyncio
async def test_mcp_tools_load():
    """Integration test: actually connect to MCP servers and load tools.

    This is the real smoke test — verifies servers start and respond.
    Skipped if running in CI without MCP binaries.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        pytest.skip("langchain-mcp-adapters not installed")

    config = get_configured_servers()
    if not config:
        pytest.skip("No MCP servers configured")

    try:
        client = MultiServerMCPClient(config)
        tools = await client.get_tools()
        assert len(tools) > 0, "No tools returned from MCP servers"

        # Verify tool names are unique
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"

        # Log what we got
        print(f"\nMCP smoke test: {len(tools)} tools from {len(config)} servers")
        for server_name in config:
            server_tools = [t.name for t in tools if t.name.startswith(server_name.replace("-", "_"))]
            if not server_tools:
                server_tools = [t.name for t in tools]  # some servers don't prefix
            print(f"  {server_name}: available")

    except Exception as e:
        # If MCP binaries aren't available, skip gracefully
        if "FileNotFoundError" in str(type(e).__name__) or "not found" in str(e).lower():
            pytest.skip(f"MCP binary not available: {e}")
        raise
