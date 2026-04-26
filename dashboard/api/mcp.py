"""MCP Server Management API."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.config import settings
from kronos.tools.mcp_servers import build_mcp_config

router = APIRouter(prefix="/api/mcp", tags=["mcp"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.mcp")

OVERRIDES_FILE = Path(settings.db_path).parent / "mcp_overrides.json"


def _load_overrides() -> dict:
    if OVERRIDES_FILE.exists():
        return json.loads(OVERRIDES_FILE.read_text())
    return {}


def _save_overrides(data: dict) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


class McpServerInput(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    description: str = ""


@router.get("/servers")
async def list_servers():
    """List all MCP servers with config and override status."""
    base_config = build_mcp_config()
    overrides = _load_overrides()

    servers = []
    for name, config in base_config.items():
        disabled = overrides.get(name, {}).get("disabled", False)
        servers.append({
            "name": name,
            "transport": config.get("transport", "stdio"),
            "command": config.get("command", ""),
            "args": config.get("args", []),
            "source": "builtin",
            "disabled": disabled,
        })

    # Add override-only servers
    for name, override in overrides.items():
        if name not in base_config and not override.get("disabled"):
            servers.append({
                "name": name,
                "transport": override.get("transport", "stdio"),
                "command": override.get("command", ""),
                "args": override.get("args", []),
                "source": "custom",
                "disabled": False,
            })

    return {"servers": servers}


@router.post("/servers")
async def add_server(server: McpServerInput):
    """Add a custom MCP server via overrides."""
    overrides = _load_overrides()
    overrides[server.name] = {
        "transport": "stdio",
        "command": server.command,
        "args": server.args,
        "env": server.env,
        "description": server.description,
    }
    _save_overrides(overrides)
    log.info("MCP server added via override: %s", server.name)
    return {"ok": True, "name": server.name}


@router.post("/servers/{name}/toggle")
async def toggle_server(name: str):
    """Enable/disable an MCP server."""
    overrides = _load_overrides()
    current = overrides.get(name, {})
    current["disabled"] = not current.get("disabled", False)
    overrides[name] = current
    _save_overrides(overrides)
    state = "disabled" if current["disabled"] else "enabled"
    log.info("MCP server %s: %s", name, state)
    return {"ok": True, "name": name, "disabled": current["disabled"]}


@router.delete("/servers/{name}")
async def delete_server(name: str):
    """Remove a custom MCP server."""
    overrides = _load_overrides()
    if name in overrides:
        del overrides[name]
        _save_overrides(overrides)
    return {"ok": True}
