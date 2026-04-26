"""Agents Management API."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.agents")

REGISTRY_FILE = Path(settings.db_path).parent / "agent_registry.json"

# Default registry (matches current hardcoded agents)
DEFAULT_REGISTRY = {
    "deep_research_agent": {
        "enabled": True,
        "module": "kronos.agents.deep_research.graph",
        "factory": "create_deep_research_agent",
        "tool_prefixes": ["brave", "exa", "fetch", "content", "extract", "reddit", "search", "transcript"],
        "tier": "standard",
        "description": "Deep research: multi-step pipeline (plan, search, evaluate, synthesize). Modes: topic, validation, market, competitive, trends.",
    },
    "research_agent": {
        "enabled": True,
        "module": "kronos.agents.research",
        "factory": "create_research_agent",
        "tool_prefixes": ["brave", "exa", "fetch", "content", "reddit", "extract"],
        "tier": "standard",
        "description": "Quick web search + content extraction + synthesis.",
    },
    "task_agent": {
        "enabled": True,
        "module": "kronos.agents.task",
        "factory": "create_task_agent",
        "tool_prefixes": ["notion", "google", "workspace", "gmail", "calendar", "filesystem", "read_file", "write_file", "list"],
        "tier": "lite",
        "description": "Notion, calendar, email, filesystem operations.",
    },
    "finance_agent": {
        "enabled": True,
        "module": "kronos.agents.finance",
        "factory": "create_finance_agent",
        "tool_prefixes": ["yahoo", "stock", "finance", "market", "brave"],
        "tier": "standard",
        "description": "Stock prices, financial analysis, market data.",
    },
}


def _load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return dict(DEFAULT_REGISTRY)


def _save_registry(data: dict) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


class AgentUpdate(BaseModel):
    enabled: bool | None = None
    description: str | None = None
    tier: str | None = None
    tool_prefixes: list[str] | None = None


@router.get("/")
async def list_agents():
    """List all agents with config."""
    registry = _load_registry()
    agents = []
    for name, config in registry.items():
        agents.append({"name": name, **config})
    return {"agents": agents}


@router.get("/{name}")
async def get_agent(name: str):
    """Get agent details."""
    registry = _load_registry()
    if name not in registry:
        raise HTTPException(404, f"Agent not found: {name}")
    return {"name": name, **registry[name]}


@router.put("/{name}")
async def update_agent(name: str, body: AgentUpdate):
    """Update agent config."""
    registry = _load_registry()
    if name not in registry:
        raise HTTPException(404, f"Agent not found: {name}")
    if body.enabled is not None:
        registry[name]["enabled"] = body.enabled
    if body.description is not None:
        registry[name]["description"] = body.description
    if body.tier is not None:
        registry[name]["tier"] = body.tier
    if body.tool_prefixes is not None:
        registry[name]["tool_prefixes"] = body.tool_prefixes
    _save_registry(registry)
    log.info("Agent updated: %s", name)
    return {"ok": True, "name": name, **registry[name]}


@router.post("/{name}/toggle")
async def toggle_agent(name: str):
    """Enable/disable an agent."""
    registry = _load_registry()
    if name not in registry:
        raise HTTPException(404, f"Agent not found: {name}")
    registry[name]["enabled"] = not registry[name]["enabled"]
    _save_registry(registry)
    state = "enabled" if registry[name]["enabled"] else "disabled"
    log.info("Agent %s: %s", name, state)
    return {"ok": True, "name": name, "enabled": registry[name]["enabled"]}
