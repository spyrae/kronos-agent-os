"""Persona API — CRUD for workspace files (Three-Space layout)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos import evolution
from kronos.config import settings
from kronos.persona import build_system_prompt
from kronos.workspace import ws

router = APIRouter(prefix="/api/persona", tags=["persona"], dependencies=[Depends(verify_token)])

# Map display name -> actual path
EDITABLE_FILES: dict[str, Path] = {
    "IDENTITY.md": ws.identity,
    "SOUL.md": ws.soul,
    "USER.md": ws.user,
    "MEMORY.md": ws.memory,
    "TOOLS.md": ws.tools,
    "AGENTS.md": ws.agents,
    "HEARTBEAT.md": ws.heartbeat,
}


class FileContent(BaseModel):
    content: str


@router.get("/files")
async def list_files():
    """List all persona files with preview."""
    files = []
    for name, path in EDITABLE_FILES.items():
        if path.exists():
            content = path.read_text(encoding="utf-8")
            files.append(
                {
                    "name": name,
                    "size": len(content),
                    "preview": content[:200],
                }
            )
    return {"files": files}


@router.get("/files/{filename}")
async def get_file(filename: str):
    """Get full content of a persona file."""
    path = EDITABLE_FILES.get(filename)
    if not path:
        raise HTTPException(404, f"File not editable: {filename}")
    if not path.exists():
        raise HTTPException(404, f"File not found: {filename}")
    return {"name": filename, "content": path.read_text(encoding="utf-8")}


@router.put("/files/{filename}")
async def update_file(filename: str, body: FileContent):
    """Update a persona file."""
    path = EDITABLE_FILES.get(filename)
    if not path:
        raise HTTPException(404, f"File not editable: {filename}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return {"ok": True, "name": filename, "size": len(body.content)}


@router.get("/preview")
async def preview_prompt():
    """Preview the assembled system prompt."""
    prompt = build_system_prompt()
    return {"prompt": prompt, "length": len(prompt)}


# --- Self-improvement proposals (roadmap 6.3) ---
# Same semantics as the Telegram /persona command: approve applies the
# proposal to the workspace file and counts the swarm metric.


class ProposalDecision(BaseModel):
    approved: bool


@router.get("/proposals")
async def list_proposals():
    """Pending persona evolution proposals for this agent."""
    pending = evolution.list_pending(settings.agent_name)
    return {"proposals": pending, "pending": len(pending)}


@router.post("/proposals/{proposal_id}/decision")
async def decide_proposal(proposal_id: int, body: ProposalDecision):
    """Approve (and apply) or reject a persona evolution proposal."""
    decided = evolution.decide_proposal(proposal_id, settings.agent_name, approved=body.approved)
    if decided is None:
        raise HTTPException(404, "Proposal not found or already decided")
    if not body.approved:
        return {"ok": True, "id": proposal_id, "status": "rejected"}

    path = evolution.apply_proposal(decided)
    from kronos.swarm_store import get_swarm

    get_swarm().incr_metric("persona_proposals_approved")
    return {"ok": True, "id": proposal_id, "status": "approved", "applied_to": path}
