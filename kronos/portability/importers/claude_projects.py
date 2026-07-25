"""Import a Claude project (directory export or `projects.json`).

Mapping choices:

* project instructions (`CLAUDE.md`, `instructions.md`, `project_instructions.md`)
  become a persona draft — that file is the closest thing a Claude project has to
  an identity;
* `skills/<name>/SKILL.md` maps to KAOS skills one-to-one, references included;
* every other document becomes a note.

Detection is deliberately strict (an instructions file, a skills directory, or a
`projects.json`) so a plain markdown folder is still recognised as an Obsidian
vault rather than being claimed here.
"""

import json
import logging
from pathlib import Path

from kronos.portability.build import BundleBuilder
from kronos.portability.manifest import BundleError

log = logging.getLogger("kronos.portability.importers.claude_projects")

NAME = "claude-projects"

_INSTRUCTION_FILES = ("CLAUDE.md", "project_instructions.md", "instructions.md")
_PROJECTS_JSON = "projects.json"
_DOC_SUFFIXES = (".md", ".txt")
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".obsidian"})
_MAX_DOC_BYTES = 1024 * 1024
_MAX_JSON_BYTES = 64 * 1024 * 1024


def detect(path: Path) -> bool:
    """True for a project directory with instructions/skills, or a projects.json."""
    if path.is_file():
        return path.name == _PROJECTS_JSON
    if not path.is_dir():
        return False
    if (path / _PROJECTS_JSON).exists():
        return True
    if (path / "skills").is_dir():
        return True
    return any((path / name).exists() for name in _INSTRUCTION_FILES)


def _add_skills(builder: BundleBuilder, skills_root: Path) -> None:
    if not skills_root.is_dir():
        return
    for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        references = {}
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            for ref in sorted(p for p in refs_dir.rglob("*") if p.is_file() and p.suffix in _DOC_SUFFIXES):
                references[ref.relative_to(refs_dir).as_posix()] = ref.read_text(encoding="utf-8")
        builder.add_skill(skill_dir.name, skill_md.read_text(encoding="utf-8"), references)


def _from_directory(builder: BundleBuilder, root: Path) -> None:
    for name in _INSTRUCTION_FILES:
        candidate = root / name
        if candidate.exists():
            builder.add_persona("IDENTITY.md", candidate.read_text(encoding="utf-8"))
            break

    _add_skills(builder, root / "skills")

    for doc in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES):
        relative = doc.relative_to(root)
        if any(part in _SKIP_DIRS or part == "skills" for part in relative.parts[:-1]):
            continue
        if relative.name in _INSTRUCTION_FILES and len(relative.parts) == 1:
            continue
        if doc.stat().st_size > _MAX_DOC_BYTES:
            builder.warnings.append(f"skipped oversized document: {relative.as_posix()}")
            continue
        try:
            builder.add_note(f"world/claude-project/{relative.as_posix()}", doc.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            builder.warnings.append(f"skipped non-text document: {relative.as_posix()}")


def _from_projects_json(builder: BundleBuilder, path: Path) -> None:
    if path.stat().st_size > _MAX_JSON_BYTES:
        raise BundleError(f"{path.name} is above the {_MAX_JSON_BYTES // 1024 // 1024} MB import limit")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BundleError(f"{path.name} is not valid JSON: {e}") from e

    projects = raw if isinstance(raw, list) else raw.get("projects", [])
    if not isinstance(projects, list):
        raise BundleError(f"{path.name} does not contain a list of projects")

    for project in projects:
        if not isinstance(project, dict):
            continue
        slug = str(project.get("name") or project.get("uuid") or "project").strip() or "project"
        instructions = str(project.get("prompt_template") or project.get("instructions") or "").strip()
        if instructions and "IDENTITY.md" not in builder.persona:
            builder.add_persona("IDENTITY.md", instructions)

        description = str(project.get("description") or "").strip()
        if description:
            builder.add_note(f"world/claude-project/{slug}/README.md", f"# {slug}\n\n{description}\n")

        for doc in project.get("docs") or []:
            if not isinstance(doc, dict):
                continue
            filename = str(doc.get("filename") or doc.get("name") or "document.md").strip()
            content = str(doc.get("content") or "")
            if content:
                builder.add_note(f"world/claude-project/{slug}/{filename}", content)


def to_bundle(
    path: str | Path,
    out_path: str | Path,
    *,
    limit: int | None = None,
    user_id: str = "",
    created_at: str = "",
):
    """Convert a Claude project export into a bundle at ``out_path``."""
    from kronos.portability.importers import ImporterResult

    source = Path(path)
    builder = BundleBuilder(agent_name=NAME, created_at=created_at)

    if source.is_file() and source.name == _PROJECTS_JSON:
        _from_projects_json(builder, source)
    elif source.is_dir():
        if (source / _PROJECTS_JSON).exists():
            _from_projects_json(builder, source / _PROJECTS_JSON)
        _from_directory(builder, source)
    else:
        raise BundleError(f"not a Claude project export: {source}")

    if limit is not None and len(builder.notes) > limit:
        keep = dict(sorted(builder.notes.items())[:limit])
        dropped = len(builder.notes) - len(keep)
        builder.notes = keep
        builder.warnings.append(f"limited to {limit} documents, {dropped} skipped")

    if builder.is_empty():
        raise BundleError(f"no importable content found in {source}")

    bundle, manifest = builder.write(out_path)
    log.info("Claude project import: %s", builder.counts())
    return ImporterResult(
        importer=NAME,
        bundle=bundle,
        manifest=manifest,
        counts=builder.counts(),
        warnings=builder.warnings,
    )
