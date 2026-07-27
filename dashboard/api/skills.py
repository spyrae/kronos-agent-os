"""Skills Management API."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dashboard.auth import verify_token
from kronos.config import settings

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.skills")


def _skills_root() -> Path:
    """Skills directory the RUNTIME actually reads.

    The agent's SkillStore loads from ``Workspace.skills_dir`` =
    ``<workspace>/self/skills``. The dashboard previously used
    ``<workspace>/skills``, so edits made here were invisible to the running
    agent. Keep this aligned with kronos.workspace.Workspace.skills_dir.
    """
    return Path(settings.workspace_path) / "self" / "skills"


class SkillContent(BaseModel):
    content: str


def _provenance() -> dict[str, dict]:
    """Version, proof and usage per skill, keyed by directory name.

    Read through the store and the usage counter so the control room shows the
    same facts as `kaos skills verify` and `kaos skills stats`. Best-effort: a
    disabled or half-written skill must still list.
    """
    try:
        from kronos.skills.store import SkillStore
        from kronos.skills.usage import local_report

        # Built from the same workspace the listing reads, not from the process-wide
        # singleton: otherwise the two halves of a row could describe different
        # agents' skills.
        store = SkillStore(str(_skills_root().parent.parent))
        return {row["skill"]: row for row in local_report(store)}
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not read skill provenance: %s", e)
        return {}


@router.get("/")
async def list_skills():
    """List all skills with enabled status, provenance and usage."""
    skills_dir = _skills_root()
    if not skills_dir.is_dir():
        return {"skills": []}

    provenance = _provenance()
    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / "SKILL.md"
        disabled_file = skill_dir / "SKILL.md.disabled"
        if skill_file.exists():
            content, enabled = skill_file.read_text(encoding="utf-8"), True
        elif disabled_file.exists():
            content, enabled = disabled_file.read_text(encoding="utf-8"), False
        else:
            continue

        row = provenance.get(skill_dir.name, {})
        skills.append(
            {
                "name": skill_dir.name,
                "enabled": enabled,
                "size": len(content),
                "preview": content[:150],
                "version": row.get("version", ""),
                "status": row.get("status", ""),
                # "verified" means a checksum exists and this is what it covers;
                # "signed" means a configured key vouched for it.
                "verified": bool(row.get("verified", False)),
                "signed": bool(row.get("signed", False)),
                "eval_status": row.get("eval_status", "none"),
                "calls": int(row.get("calls", 0) or 0),
            }
        )

    return {"skills": skills}


@router.get("/{name}")
async def get_skill(name: str):
    """Get skill content."""
    skills_dir = _skills_root() / name
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
    skills_dir = _skills_root() / name
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
    skills_dir = _skills_root() / name
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
    skills_dir = _skills_root() / body.name
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
    skills_dir = _skills_root() / name
    if not skills_dir.exists():
        raise HTTPException(404, f"Skill not found: {name}")
    import shutil

    shutil.rmtree(skills_dir)
    log.info("Skill deleted: %s", name)
    return {"ok": True, "name": name}
