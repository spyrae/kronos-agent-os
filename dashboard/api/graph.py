"""Agent Architecture API — introspect agent structure."""

import logging

from fastapi import APIRouter, Depends

from dashboard.auth import verify_token

router = APIRouter(prefix="/api/graph", tags=["graph"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.graph")

# Set from server.py when agent is available
_agent = None


def set_agent(agent) -> None:
    global _agent
    _agent = agent


def _live_agent_names() -> list[str]:
    """Delegation targets the running supervisor actually exposes.

    Derived from the live supervisor's tools (``delegate_to_X``), so the diagram
    reflects reality — which agents exist and the operator's registry toggles —
    instead of a hardcoded list that drifts from the running system.
    """
    supervisor = getattr(_agent, "_supervisor", None)
    names: list[str] = []
    for tool in getattr(supervisor, "_approval_tools", []) or []:
        name = getattr(tool, "name", "")
        if name.startswith("delegate_to_"):
            names.append(name[len("delegate_to_") :])
    return names


@router.get("/structure")
async def get_structure():
    """Get agent pipeline structure for visualization."""
    if not _agent:
        return {"nodes": [], "edges": []}

    # Fixed react_loop engine stages; agent nodes come from the live supervisor.
    nodes = [
        {"id": "validate", "label": "Validate Input", "type": "security"},
        {"id": "retrieve_memories", "label": "Retrieve Memories", "type": "memory"},
        {"id": "supervisor", "label": "Supervisor Router", "type": "router"},
    ]
    edges = [
        {"source": "validate", "target": "retrieve_memories", "conditional": False},
        {"source": "retrieve_memories", "target": "supervisor", "conditional": False},
    ]

    for name in _live_agent_names():
        label = name.replace("_", " ").title()
        nodes.append({"id": name, "label": f"{label} Agent", "type": "agent"})
        edges.append({"source": "supervisor", "target": name, "conditional": True})
        edges.append({"source": name, "target": "store_memories", "conditional": False})

    nodes.append({"id": "store_memories", "label": "Store Memories", "type": "memory"})
    nodes.append({"id": "compact", "label": "Compact History", "type": "memory"})
    edges.append({"source": "supervisor", "target": "store_memories", "conditional": True})
    edges.append({"source": "store_memories", "target": "compact", "conditional": True})

    return {"nodes": nodes, "edges": edges}


@router.get("/mermaid")
async def get_mermaid():
    """Get Mermaid diagram of the agent pipeline (live agents)."""
    if not _agent:
        return {"mermaid": ""}

    lines = [
        "graph TD",
        "  validate[Validate Input] --> retrieve_memories[Retrieve Memories]",
        "  retrieve_memories --> supervisor{Supervisor Router}",
    ]
    for name in _live_agent_names():
        label = name.replace("_", " ").title()
        lines.append(f"  supervisor -->|{name}| {name}[{label}]")
        lines.append(f"  {name} --> store_memories")
    lines.append("  supervisor -->|direct| store_memories[Store Memories]")
    lines.append("  store_memories -->|if needed| compact[Compact History]")

    return {"mermaid": "\n".join(lines)}
