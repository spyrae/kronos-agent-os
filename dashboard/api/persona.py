"""Persona API — CRUD for workspace files (Three-Space layout)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
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
            files.append({
                "name": name,
                "size": len(content),
                "preview": content[:200],
            })
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
