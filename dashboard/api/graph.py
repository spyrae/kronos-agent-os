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


@router.get("/structure")
async def get_structure():
    """Get agent pipeline structure for visualization."""
    if not _agent:
        return {"nodes": [], "edges": []}

    # Static pipeline description (no LangGraph introspection needed)
    nodes = [
        {"id": "validate", "label": "Validate Input", "type": "security"},
        {"id": "retrieve_memories", "label": "Retrieve Memories", "type": "memory"},
        {"id": "supervisor", "label": "Supervisor Router", "type": "router"},
        {"id": "research", "label": "Research Agent", "type": "agent"},
        {"id": "task", "label": "Task Agent", "type": "agent"},
        {"id": "finance", "label": "Finance Agent", "type": "agent"},
        {"id": "deep_research", "label": "Deep Research", "type": "agent"},
        {"id": "topic_research", "label": "Topic Research", "type": "agent"},
        {"id": "telegram_channels", "label": "Telegram Channels", "type": "agent"},
        {"id": "store_memories", "label": "Store Memories", "type": "memory"},
        {"id": "compact", "label": "Compact History", "type": "memory"},
    ]

    edges = [
        {"source": "validate", "target": "retrieve_memories", "conditional": False},
        {"source": "retrieve_memories", "target": "supervisor", "conditional": False},
        {"source": "supervisor", "target": "research", "conditional": True},
        {"source": "supervisor", "target": "task", "conditional": True},
        {"source": "supervisor", "target": "finance", "conditional": True},
        {"source": "supervisor", "target": "deep_research", "conditional": True},
        {"source": "supervisor", "target": "topic_research", "conditional": True},
        {"source": "supervisor", "target": "telegram_channels", "conditional": True},
        {"source": "supervisor", "target": "store_memories", "conditional": True},
        {"source": "research", "target": "store_memories", "conditional": False},
        {"source": "task", "target": "store_memories", "conditional": False},
        {"source": "finance", "target": "store_memories", "conditional": False},
        {"source": "deep_research", "target": "store_memories", "conditional": False},
        {"source": "topic_research", "target": "store_memories", "conditional": False},
        {"source": "telegram_channels", "target": "store_memories", "conditional": False},
        {"source": "store_memories", "target": "compact", "conditional": True},
    ]

    return {"nodes": nodes, "edges": edges}


@router.get("/mermaid")
async def get_mermaid():
    """Get Mermaid diagram of the agent pipeline."""
    mermaid = """graph TD
  validate[Validate Input] --> retrieve_memories[Retrieve Memories]
  retrieve_memories --> supervisor{Supervisor Router}
  supervisor -->|research| research[Research Agent]
  supervisor -->|task| task[Task Agent]
  supervisor -->|finance| finance[Finance Agent]
  supervisor -->|deep_research| deep_research[Deep Research]
  supervisor -->|topic_research| topic_research[Topic Research]
  supervisor -->|telegram| telegram_channels[Telegram Channels]
  supervisor -->|direct| store_memories[Store Memories]
  research --> store_memories
  task --> store_memories
  finance --> store_memories
  deep_research --> store_memories
  topic_research --> store_memories
  telegram_channels --> store_memories
  store_memories -->|if needed| compact[Compact History]"""

    return {"mermaid": mermaid}
