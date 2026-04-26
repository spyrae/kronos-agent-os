"""MCP Gateway — dynamic MCP server management with hot-reload.

Manages MCP server lifecycle: add/remove servers without restarting
the agent. Provides management tools that the agent can call.

Registry persists in SQLite so servers survive restarts.
"""

import json
import logging
import sqlite3
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from kronos.config import settings
from kronos.tools.mcp_servers import build_mcp_config

log = logging.getLogger("kronos.tools.gateway")


class MCPGateway:
    """Dynamic MCP server gateway with hot-reload support."""

    def __init__(self):
        self._static_config: dict = {}  # from mcp_servers.py
        self._dynamic_config: dict = {}  # added at runtime
        self._tools: list[BaseTool] = []
        self._db_path = Path(settings.db_path).parent / "mcp_registry.db"
        self._init_db()
        self._load_dynamic_servers()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mcp_servers (
                name TEXT PRIMARY KEY,
                config TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _load_dynamic_servers(self) -> None:
        """Load dynamically added servers from DB."""
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute(
            "SELECT name, config FROM mcp_servers WHERE enabled = 1"
        ).fetchall()
        conn.close()

        for name, config_json in rows:
            try:
                self._dynamic_config[name] = json.loads(config_json)
            except json.JSONDecodeError:
                log.error("Invalid config for MCP server '%s'", name)

        if self._dynamic_config:
            log.info("Loaded %d dynamic MCP servers from registry", len(self._dynamic_config))

    async def start(self) -> list[BaseTool]:
        """Start all MCP servers and return tools.

        Called once at startup. Returns combined tools from
        static (mcp_servers.py) + dynamic (registry) servers.
        """
        self._static_config = build_mcp_config()
        combined = {**self._static_config, **self._dynamic_config}

        if not combined:
            log.warning("No MCP servers configured")
            return []

        log.info("Starting %d MCP servers (%d static + %d dynamic)...",
                 len(combined), len(self._static_config), len(self._dynamic_config))

        client = MultiServerMCPClient(combined)
        self._tools = await client.get_tools()

        log.info("Loaded %d tools from %d servers", len(self._tools), len(combined))
        return self._tools

    def add_server(self, name: str, config: dict) -> str:
        """Add a new MCP server dynamically.

        Server is persisted in registry and available after restart.
        Note: tools won't be available until next reload.
        """
        # Validate config
        if "transport" not in config:
            return f"Error: config must include 'transport' (stdio or sse)"
        if "command" not in config and config.get("transport") == "stdio":
            return f"Error: stdio transport requires 'command'"

        # Save to DB
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "INSERT OR REPLACE INTO mcp_servers (name, config) VALUES (?, ?)",
            (name, json.dumps(config)),
        )
        conn.commit()
        conn.close()

        self._dynamic_config[name] = config
        log.info("Added MCP server '%s' to registry", name)
        return f"MCP server '{name}' added. Reload required to activate tools."

    def remove_server(self, name: str) -> str:
        """Remove a dynamic MCP server."""
        if name in self._static_config:
            return f"Cannot remove static server '{name}'. Edit mcp_servers.py instead."

        conn = sqlite3.connect(str(self._db_path))
        conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        conn.commit()
        conn.close()

        self._dynamic_config.pop(name, None)
        log.info("Removed MCP server '%s' from registry", name)
        return f"MCP server '{name}' removed."

    def list_servers(self) -> dict:
        """List all registered servers and their status."""
        result = {}
        for name in self._static_config:
            result[name] = {"source": "static", "enabled": True}
        for name in self._dynamic_config:
            result[name] = {"source": "dynamic", "enabled": True}
        return result

    async def reload(self) -> str:
        """Reload all tools from all servers."""
        combined = {**self._static_config, **self._dynamic_config}
        if not combined:
            return "No servers configured."

        try:
            client = MultiServerMCPClient(combined)
            self._tools = await client.get_tools()
            msg = f"Reloaded: {len(self._tools)} tools from {len(combined)} servers"
            log.info(msg)
            return msg
        except Exception as e:
            msg = f"Reload failed: {e}"
            log.error(msg)
            return msg

    def get_tools(self) -> list[BaseTool]:
        """Get currently loaded tools."""
        return self._tools


# Singleton
_gateway: MCPGateway | None = None


def get_gateway() -> MCPGateway:
    global _gateway
    if _gateway is None:
        _gateway = MCPGateway()
    return _gateway
