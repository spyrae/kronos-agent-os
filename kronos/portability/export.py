"""Export an agent as a `.kaos` bundle.

What goes in: persona files, local skills, extracted facts, knowledge graph,
this agent's shared facts, pending schedule, and — opt-in — notes and session
history.

What never goes in, under any flag: `.env*`, Telegram `*.session`, SQLite files
themselves, the Qdrant vector store (re-embedded on import), and audit logs.
The blocklist is enforced in ``_is_exportable`` and covered by a negative test.

Databases are read through a read-only SQLite URI so exporting never creates a
database that did not already exist and never blocks a live writer.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from kronos.config import settings
from kronos.portability.build import write_jsonl, write_zip
from kronos.portability.dbread import read_rows
from kronos.portability.manifest import (
    SECTION_FACTS,
    SECTION_GRAPH,
    SECTION_NOTES,
    SECTION_PERSONA,
    SECTION_SCHEDULE,
    SECTION_SESSIONS,
    SECTION_SHARED_FACTS,
    SECTION_SKILLS,
    BundleManifest,
    build_manifest,
    write_manifest,
)
from kronos.portability.redact import redact_structure, redact_text
from kronos.workspace import Workspace

log = logging.getLogger("kronos.portability.export")

# Files that must never leave the machine, regardless of location or flags.
_BLOCKED_NAMES = frozenset({".env", ".envrc"})
_BLOCKED_SUFFIXES = (".session", ".session-journal", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".pem", ".key")
_BLOCKED_PREFIXES = (".env",)
_BLOCKED_DIR_NAMES = frozenset({"qdrant", ".git", "__pycache__", "logs"})

# Text-ish payloads we are willing to copy verbatim.
_ALLOWED_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv")

_MAX_COPY_FILE_BYTES = 5 * 1024 * 1024
_LARGE_BUNDLE_WARN_BYTES = 50 * 1024 * 1024


@dataclass
class ExportReport:
    """Outcome of one export."""

    path: Path
    manifest: BundleManifest
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return self.manifest.counts


def _is_exportable(path: Path) -> bool:
    """Whether a file may be copied into a bundle."""
    if any(part in _BLOCKED_DIR_NAMES for part in path.parts):
        return False
    name = path.name
    if name in _BLOCKED_NAMES or name.startswith(_BLOCKED_PREFIXES):
        return False
    if name.endswith(_BLOCKED_SUFFIXES):
        return False
    return path.suffix.lower() in _ALLOWED_SUFFIXES


def _copy_text_file(src: Path, dest: Path, *, warnings: list[str]) -> bool:
    """Copy one file with credential redaction. Returns False if skipped."""
    if not _is_exportable(src):
        return False
    if src.stat().st_size > _MAX_COPY_FILE_BYTES:
        warnings.append(f"skipped oversized file: {src.name} (>{_MAX_COPY_FILE_BYTES // 1024 // 1024} MB)")
        return False
    try:
        content = src.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        warnings.append(f"skipped non-text file: {src.name}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(redact_text(content), encoding="utf-8")
    return True


def _export_persona(workspace: Workspace, root: Path, *, warnings: list[str]) -> int:
    """Copy the four persona files that define who the agent is."""
    count = 0
    for source in (workspace.identity, workspace.soul, workspace.agents, workspace.methodology):
        if source.exists() and _copy_text_file(source, root / "persona" / source.name, warnings=warnings):
            count += 1
    return count


def _export_skills(workspace: Workspace, root: Path, *, warnings: list[str]) -> int:
    """Copy local skills (SKILL.md plus references). Shared skills stay behind.

    A shared workspace belongs to the deployment, not to this agent, so
    exporting it would hand over someone else's material.
    """
    skills_dir = workspace.skills_dir
    if not skills_dir.exists():
        return 0

    count = 0
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        if not _copy_text_file(skill_md, root / "skills" / skill_dir.name / "SKILL.md", warnings=warnings):
            continue
        count += 1
        refs = skill_dir / "references"
        if refs.is_dir():
            for ref in sorted(p for p in refs.rglob("*") if p.is_file()):
                _copy_text_file(
                    ref,
                    root / "skills" / skill_dir.name / "references" / ref.relative_to(refs),
                    warnings=warnings,
                )
    return count


def _export_facts(root: Path, *, warnings: list[str]) -> int:
    """Dump FTS-indexed facts, ordered for reproducible hashes."""
    db_path = Path(settings.db_dir) / "memory_fts.db"
    rows = read_rows(db_path, "SELECT * FROM memory_facts ORDER BY created_at, content")
    if not rows:
        warnings.append("no extracted facts found")

    def _payload() -> Iterable[dict]:
        for row in rows:
            keys = row.keys()
            fact = {
                "user_id": row["user_id"],
                "content": redact_text(row["content"]),
                "source": row["source"] if "source" in keys else "",
                "created_at": row["created_at"],
            }
            # relevance arrived with the Ebbinghaus decay migration; mem0_id is a
            # local vector pointer and is meaningless in another installation.
            if "relevance" in keys and row["relevance"] is not None:
                fact["relevance"] = row["relevance"]
            yield fact

    return write_jsonl(root / "memory" / "facts.jsonl", _payload())


def _export_graph(root: Path) -> tuple[int, int]:
    """Dump entities and relations, with relations keyed by (name, type)."""
    db_path = Path(settings.db_dir) / "knowledge_graph.db"

    entities = read_rows(db_path, "SELECT name, type, properties FROM entities ORDER BY type, name")
    entity_rows = [
        {"name": row["name"], "type": row["type"], "properties": _json_or_empty(row["properties"])} for row in entities
    ]

    relations = read_rows(
        db_path,
        """
        SELECT s.name AS source_name, s.type AS source_type,
               t.name AS target_name, t.type AS target_type,
               r.relation_type, r.properties
        FROM relations r
        JOIN entities s ON s.id = r.source_id
        JOIN entities t ON t.id = r.target_id
        ORDER BY s.type, s.name, r.relation_type, t.type, t.name
        """,
    )
    relation_rows = [
        {
            "source": {"name": row["source_name"], "type": row["source_type"]},
            "target": {"name": row["target_name"], "type": row["target_type"]},
            "relation_type": row["relation_type"],
            "properties": _json_or_empty(row["properties"]),
        }
        for row in relations
    ]

    written_entities = write_jsonl(root / "memory" / "graph.entities.jsonl", entity_rows)
    written_relations = write_jsonl(root / "memory" / "graph.relations.jsonl", relation_rows)
    return written_entities, written_relations


def _json_or_empty(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _export_shared_facts(root: Path) -> int:
    """Dump only facts this agent contributed to the swarm ledger."""
    rows = read_rows(
        Path(settings.swarm_db_path),
        """
        SELECT user_id, fact, created_at FROM shared_user_facts
        WHERE source_agent = ?
        ORDER BY created_at, fact
        """,
        (settings.agent_name,),
    )
    payload = [
        {"user_id": row["user_id"], "fact": redact_text(row["fact"]), "created_at": row["created_at"]} for row in rows
    ]
    return write_jsonl(root / "memory" / "shared_facts.jsonl", payload)


def _export_schedule(root: Path, *, include_transport_ids: bool) -> int:
    """Dump pending reminders/follow-ups.

    Transport ids identify someone's private chats, so by default they are
    dropped and the task is marked ``needs_rebind`` — the importer then knows the
    task cannot fire until it is attached to a local chat.
    """
    rows = read_rows(
        Path(settings.db_dir) / "scheduled_tasks.db",
        """
        SELECT chat_id, topic_id, thread_id, run_at, recur_seconds, message, kind
        FROM scheduled_tasks
        WHERE agent_name = ? AND status = 'pending'
        ORDER BY run_at, message
        """,
        (settings.agent_name,),
    )

    payload = []
    for row in rows:
        task = {
            "run_at": row["run_at"],
            "recur_seconds": row["recur_seconds"],
            "message": redact_text(row["message"]),
            "kind": row["kind"],
        }
        if include_transport_ids:
            task |= {
                "chat_id": row["chat_id"],
                "topic_id": row["topic_id"],
                "thread_id": row["thread_id"],
                "needs_rebind": False,
            }
        else:
            task |= {"chat_id": None, "topic_id": None, "thread_id": None, "needs_rebind": True}
        payload.append(task)

    return write_jsonl(root / "schedule" / "tasks.jsonl", payload)


def _export_notes(workspace: Workspace, root: Path, *, warnings: list[str]) -> int:
    """Copy markdown notes (owner-authored knowledge)."""
    notes_dir = workspace.notes_dir
    if not notes_dir.exists():
        return 0
    count = 0
    for note in sorted(p for p in notes_dir.rglob("*.md") if p.is_file()):
        if _copy_text_file(note, root / "notes" / note.relative_to(notes_dir), warnings=warnings):
            count += 1
    return count


def _export_sessions(root: Path) -> int:
    """Dump conversation history, one JSONL row per thread.

    Session content is third-party material and tool output, so it is masked
    with ``redact_structure`` rather than the lighter owner-content redaction.
    A single file avoids turning thread ids — which contain ``:`` — into
    filesystem-hostile filenames.
    """
    rows = read_rows(
        Path(settings.db_path),
        "SELECT thread_id, messages, updated_at FROM sessions ORDER BY thread_id",
    )

    payload = []
    for row in rows:
        try:
            messages = json.loads(row["messages"] or "[]")
        except json.JSONDecodeError:
            continue
        payload.append(
            {
                "thread_id": row["thread_id"],
                "updated_at": str(row["updated_at"]),
                "messages": redact_structure(messages, mask_personal=True),
            }
        )

    return write_jsonl(root / "sessions" / "sessions.jsonl", payload)


def export_bundle(
    out_path: str | Path,
    *,
    include_notes: bool = False,
    include_sessions: bool = False,
    include_transport_ids: bool = False,
    created_at: str = "",
    workspace: Workspace | None = None,
) -> ExportReport:
    """Export the configured agent into a `.kaos` bundle at ``out_path``."""
    target = Path(out_path)
    # Resolved here, not at import time: tests and multi-agent hosts swap
    # kronos.workspace.ws, and a module-level binding would freeze the first one.
    from kronos.workspace import ws

    space = workspace or ws
    warnings: list[str] = []
    counts: dict[str, int] = {}
    includes = [
        SECTION_PERSONA,
        SECTION_SKILLS,
        SECTION_FACTS,
        SECTION_GRAPH,
        SECTION_SHARED_FACTS,
        SECTION_SCHEDULE,
    ]

    with TemporaryDirectory(prefix="kaos-export-") as staging:
        root = Path(staging)

        counts["persona_files"] = _export_persona(space, root, warnings=warnings)
        counts["skills"] = _export_skills(space, root, warnings=warnings)
        counts["facts"] = _export_facts(root, warnings=warnings)
        counts["graph_entities"], counts["graph_relations"] = _export_graph(root)
        counts["shared_facts"] = _export_shared_facts(root)
        counts["scheduled_tasks"] = _export_schedule(root, include_transport_ids=include_transport_ids)

        if include_notes:
            counts["notes"] = _export_notes(space, root, warnings=warnings)
            includes.append(SECTION_NOTES)
        if include_sessions:
            counts["sessions"] = _export_sessions(root)
            includes.append(SECTION_SESSIONS)

        manifest = build_manifest(
            root,
            agent_name=settings.agent_name,
            includes=includes,
            counts=counts,
            created_at=created_at,
        )
        write_manifest(root, manifest)
        write_zip(root, target)

    size = target.stat().st_size
    if size > _LARGE_BUNDLE_WARN_BYTES:
        warnings.append(f"bundle is {size // 1024 // 1024} MB — consider exporting without sessions/notes")

    log.info("Exported bundle: %s (%d bytes, sections: %s)", target, size, ", ".join(manifest.includes))
    return ExportReport(path=target, manifest=manifest, warnings=warnings)
