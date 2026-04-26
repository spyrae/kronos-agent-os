"""Skills Management API."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.skills")


class SkillContent(BaseModel):
    content: str


@router.get("/")
async def list_skills():
    """List all skills with enabled status."""
    skills_dir = Path(settings.workspace_path) / "skills"
    if not skills_dir.is_dir():
        return {"skills": []}

    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / "SKILL.md"
        disabled_file = skill_dir / "SKILL.md.disabled"

        if skill_file.exists():
            content = skill_file.read_text(encoding="utf-8")
            skills.append({
                "name": skill_dir.name,
                "enabled": True,
                "size": len(content),
                "preview": content[:150],
            })
        elif disabled_file.exists():
            content = disabled_file.read_text(encoding="utf-8")
            skills.append({
                "name": skill_dir.name,
                "enabled": False,
                "size": len(content),
                "preview": content[:150],
            })

    return {"skills": skills}


@router.get("/{name}")
async def get_skill(name: str):
    """Get skill content."""
    skills_dir = Path(settings.workspace_path) / "skills" / name
    skill_file = skills_dir / "SKILL.md"
    disabled_file = skills_dir / "SKILL.md.disabled"

    if skill_file.exists():
        return {"name": name, "enabled": True, "content": skill_file.read_text(encoding="utf-8")}
    elif disabled_file.exists():
        return {"name": name, "enabled": False, "content": disabled_file.read_text(encoding="utf-8")}
    raise HTTPException(404, f"Skill not found: {name}")


@router.put("/{name}")
async def update_skill(name: str, body: SkillContent):
    """Update skill content."""
    skills_dir = Path(settings.workspace_path) / "skills" / name
    skill_file = skills_dir / "SKILL.md"
    disabled_file = skills_dir / "SKILL.md.disabled"

    target = skill_file if skill_file.exists() else disabled_file
    if not target.exists():
        raise HTTPException(404, f"Skill not found: {name}")

    target.write_text(body.content, encoding="utf-8")
    log.info("Skill updated: %s (%d chars)", name, len(body.content))
    return {"ok": True, "name": name}


@router.post("/{name}/toggle")
async def toggle_skill(name: str):
    """Enable/disable a skill by renaming SKILL.md ↔ SKILL.md.disabled."""
    skills_dir = Path(settings.workspace_path) / "skills" / name
    skill_file = skills_dir / "SKILL.md"
    disabled_file = skills_dir / "SKILL.md.disabled"

    if skill_file.exists():
        skill_file.rename(disabled_file)
        log.info("Skill disabled: %s", name)
        return {"ok": True, "name": name, "enabled": False}
    elif disabled_file.exists():
        disabled_file.rename(skill_file)
        log.info("Skill enabled: %s", name)
        return {"ok": True, "name": name, "enabled": True}
    raise HTTPException(404, f"Skill not found: {name}")


class NewSkill(BaseModel):
    name: str
    content: str = ""


@router.post("/")
async def create_skill(body: NewSkill):
    """Create a new skill directory with SKILL.md."""
    skills_dir = Path(settings.workspace_path) / "skills" / body.name
    if skills_dir.exists():
        raise HTTPException(409, f"Skill already exists: {body.name}")
    skills_dir.mkdir(parents=True)
    content = body.content or f"---\nname: {body.name}\ndescription: \n---\n\n# {body.name}\n"
    (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
    log.info("Skill created: %s", body.name)
    return {"ok": True, "name": body.name}


@router.delete("/{name}")
async def delete_skill(name: str):
    """Delete a skill directory."""
    skills_dir = Path(settings.workspace_path) / "skills" / name
    if not skills_dir.exists():
        raise HTTPException(404, f"Skill not found: {name}")
    import shutil
    shutil.rmtree(skills_dir)
    log.info("Skill deleted: %s", name)
    return {"ok": True, "name": name}
