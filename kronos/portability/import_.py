"""Import a `.kaos` bundle into the local agent.

Design rules, in order of importance:

1. **Verify before writing.** The manifest is checked against the payload; a
   single altered byte aborts the import before any database is touched.
2. **Idempotent.** Importing the same bundle twice changes nothing the second
   time — dedupe keys are content-based, not id-based, because ids from another
   installation are meaningless here.
3. **Never activate silently.** Imported skills land as ``draft`` and persona
   files are left alone unless the caller explicitly asks otherwise: a bundle is
   someone else's material until a human reviews it.
4. **Dry-run is real.** ``dry_run=True`` produces the full report and writes
   nothing, so the same code path answers "what would this do?".
"""

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from kronos.config import settings
from kronos.portability.dbread import read_rows
from kronos.portability.manifest import (
    SECTION_NOTES,
    SECTION_SESSIONS,
    BundleError,
    BundleManifest,
    read_manifest,
    verify_manifest,
)
from kronos.skills.store import SkillStore, _parse_frontmatter
from kronos.workspace import Workspace

log = logging.getLogger("kronos.portability.import")

MERGE_SKIP = "skip"
MERGE_OVERWRITE = "overwrite"
MERGE_APPEND = "append"
MERGE_MODES = (MERGE_SKIP, MERGE_OVERWRITE, MERGE_APPEND)

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ImportReport:
    """What an import did — or would do, when dry_run is set."""

    source: Path
    source_agent: str
    merge: str
    dry_run: bool
    manifest: BundleManifest | None = None
    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def _bump(self, bucket: dict[str, int], key: str, delta: int = 1) -> None:
        bucket[key] = bucket.get(key, 0) + delta

    def add(self, key: str, delta: int = 1) -> None:
        self._bump(self.created, key, delta)

    def skip(self, key: str, delta: int = 1) -> None:
        self._bump(self.skipped, key, delta)

    @property
    def total_created(self) -> int:
        return sum(self.created.values())

    def render(self) -> str:
        """Human-readable summary for CLI output."""
        head = f"{'Would import' if self.dry_run else 'Imported'} bundle from agent '{self.source_agent}'"
        lines = [head, f"  merge mode: {self.merge}"]
        for key in sorted(set(self.created) | set(self.skipped)):
            lines.append(f"  {key}: +{self.created.get(key, 0)} new, {self.skipped.get(key, 0)} skipped")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)


def _normalize(text: str) -> str:
    """Content key for dedupe: whitespace- and case-insensitive."""
    return _WHITESPACE_RE.sub(" ", text.strip()).casefold()


def _safe_extract(archive_path: Path, dest: Path) -> None:
    """Extract a zip, refusing entries that escape the destination.

    A bundle arrives from outside; ``..`` or absolute members would let it write
    anywhere on disk.
    """
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.endswith("/"):
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise BundleError(f"bundle contains an unsafe path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as out:
                out.write(src.read())


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _import_facts(root: Path, report: ImportReport) -> None:
    """Index facts that are not already present (normalized comparison)."""
    rows = _read_jsonl(root / "memory" / "facts.jsonl")
    if not rows:
        return

    from kronos.memory import fts

    existing: dict[str, set[str]] = {}
    for row in rows:
        content = str(row.get("content", "")).strip()
        user_id = str(row.get("user_id", "")) or settings.agent_name
        if len(content) < 3:
            report.skip("facts")
            continue

        if user_id not in existing:
            db_path = Path(settings.db_dir) / "memory_fts.db"
            existing[user_id] = {
                _normalize(str(row["content"]))
                for row in read_rows(db_path, "SELECT content FROM memory_facts WHERE user_id = ?", (user_id,))
            }

        key = _normalize(content)
        if key in existing[user_id]:
            report.skip("facts")
            continue

        existing[user_id].add(key)
        report.add("facts")
        if not report.dry_run:
            fts.index_fact(content, user_id)


def _import_graph(root: Path, report: ImportReport) -> None:
    """Upsert entities and relations; both stores are already UNIQUE-keyed."""
    entities = _read_jsonl(root / "memory" / "graph.entities.jsonl")
    relations = _read_jsonl(root / "memory" / "graph.relations.jsonl")
    if not entities and not relations:
        return

    from kronos.memory import knowledge_graph

    for row in entities:
        name, entity_type = str(row.get("name", "")).strip(), str(row.get("type", "")).strip()
        if not name or not entity_type:
            report.skip("graph_entities")
            continue
        report.add("graph_entities")
        if not report.dry_run:
            knowledge_graph.add_entity(name, entity_type, row.get("properties") or {})

    for row in relations:
        source, target = row.get("source") or {}, row.get("target") or {}
        relation_type = str(row.get("relation_type", "")).strip()
        if not (source.get("name") and target.get("name") and relation_type):
            report.skip("graph_relations")
            continue
        report.add("graph_relations")
        if not report.dry_run:
            knowledge_graph.add_relation(
                str(source["name"]),
                str(source.get("type", "concept")),
                str(target["name"]),
                str(target.get("type", "concept")),
                relation_type,
                row.get("properties") or {},
            )


def _import_shared_facts(root: Path, report: ImportReport) -> None:
    """Adopt shared facts under this agent's name.

    The exporting agent does not exist in this installation, so attributing the
    fact to it would create a phantom source.
    """
    rows = _read_jsonl(root / "memory" / "shared_facts.jsonl")
    if not rows:
        return

    from kronos.swarm_store import get_swarm

    store = None if report.dry_run else get_swarm()
    for row in rows:
        fact = str(row.get("fact", "")).strip()
        user_id = str(row.get("user_id", "")) or settings.agent_name
        if not fact:
            report.skip("shared_facts")
            continue
        if report.dry_run:
            report.add("shared_facts")
            continue
        if store.add_shared_fact(user_id=user_id, fact=fact, source_agent=settings.agent_name):
            report.add("shared_facts")
        else:
            report.skip("shared_facts")


def _import_skills(root: Path, space: Workspace, report: ImportReport) -> None:
    """Install skills as drafts, honouring the merge mode for existing names."""
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return

    store = SkillStore(space.root)
    imported_at = datetime.now(UTC).isoformat(timespec="seconds")

    for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        name = skill_dir.name

        if store.get(name) and report.merge != MERGE_OVERWRITE:
            report.skip("skills")
            report.warnings.append(f"skill '{name}' already exists — kept local version")
            continue

        meta, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        meta |= {
            "name": name,
            "status": "draft",
            "imported_from": report.source_agent,
            "imported_at": imported_at,
            "review_required": True,
        }
        report.add("skills")
        if report.dry_run:
            continue

        store.add_skill(name, body, meta)
        refs_src = skill_dir / "references"
        if refs_src.is_dir():
            refs_dest = space.skills_dir / name / "references"
            refs_dest.mkdir(parents=True, exist_ok=True)
            for ref in sorted(p for p in refs_src.rglob("*") if p.is_file()):
                dest = refs_dest / ref.relative_to(refs_src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(ref.read_text(encoding="utf-8"), encoding="utf-8")
                report.add("skill_references")


def _import_persona(root: Path, space: Workspace, report: ImportReport) -> None:
    """Persona is identity — never replaced implicitly.

    ``skip`` (default) leaves local persona untouched, ``append`` adds a clearly
    marked imported section, ``overwrite`` replaces the file.
    """
    persona_dir = root / "persona"
    if not persona_dir.is_dir():
        return

    targets = {
        "IDENTITY.md": space.identity,
        "SOUL.md": space.soul,
        "AGENTS.md": space.agents,
        "methodology.md": space.methodology,
    }

    for source in sorted(p for p in persona_dir.iterdir() if p.is_file()):
        target = targets.get(source.name)
        if target is None:
            report.skip("persona_files")
            continue

        content = source.read_text(encoding="utf-8")
        if target.exists() and report.merge == MERGE_SKIP:
            report.skip("persona_files")
            report.warnings.append(f"persona {source.name} kept local (merge=skip)")
            continue

        report.add("persona_files")
        if report.dry_run:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and report.merge == MERGE_APPEND:
            stamp = f"\n\n## Imported from '{report.source_agent}' ({datetime.now(UTC).date().isoformat()})\n\n"
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(stamp + content.strip() + "\n")
        else:
            target.write_text(content, encoding="utf-8")


def _import_notes(root: Path, space: Workspace, report: ImportReport) -> None:
    """Copy notes, respecting the merge mode per file."""
    notes_root = root / "notes"
    if not notes_root.is_dir():
        return

    for note in sorted(p for p in notes_root.rglob("*.md") if p.is_file()):
        target = space.notes_dir / note.relative_to(notes_root)
        if target.exists() and report.merge == MERGE_SKIP:
            report.skip("notes")
            continue

        report.add("notes")
        if report.dry_run:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        content = note.read_text(encoding="utf-8")
        if target.exists() and report.merge == MERGE_APPEND:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(f"\n\n<!-- imported from '{report.source_agent}' -->\n\n{content.strip()}\n")
        else:
            target.write_text(content, encoding="utf-8")


def _import_schedule(root: Path, report: ImportReport, *, rebind_chat: int | None) -> None:
    """Recreate pending reminders/follow-ups.

    A task without a local chat to fire into is useless and would silently never
    deliver, so ``needs_rebind`` tasks are skipped unless ``rebind_chat`` says
    where they belong.
    """
    rows = _read_jsonl(root / "schedule" / "tasks.jsonl")
    if not rows:
        return

    from kronos import scheduled_tasks

    for row in rows:
        needs_rebind = bool(row.get("needs_rebind"))
        chat_id = rebind_chat if needs_rebind else row.get("chat_id")
        if chat_id is None:
            report.skip("scheduled_tasks")
            report.warnings.append("scheduled tasks skipped — pass rebind_chat to attach them to a chat")
            continue

        message = str(row.get("message", "")).strip()
        if not message:
            report.skip("scheduled_tasks")
            continue

        report.add("scheduled_tasks")
        if report.dry_run:
            continue

        topic_id = None if needs_rebind else row.get("topic_id")
        thread_id = str(chat_id) if needs_rebind else str(row.get("thread_id") or chat_id)
        scheduled_tasks.add_task(
            agent_name=settings.agent_name,
            chat_id=int(chat_id),
            topic_id=topic_id,
            thread_id=thread_id,
            run_at=float(row.get("run_at") or 0.0),
            message=message,
            recur_seconds=int(row.get("recur_seconds") or 0),
            kind=str(row.get("kind") or "reminder"),
        )


def _import_sessions(root: Path, space: Workspace, report: ImportReport) -> None:
    """Archive session history into notes/inbox as readable markdown.

    Deliberately NOT written into the live session store: thread ids come from
    another installation, and overwriting a real conversation's history to
    "restore" a foreign one is a data-loss bug wearing a feature's clothes. As an
    inbox note the history stays available to the agent through its own files.
    """
    sessions_file = root / "sessions" / "sessions.jsonl"
    rows = _read_jsonl(sessions_file)
    if not rows:
        return

    lines = [f"# Imported sessions from '{report.source_agent}'", ""]
    for row in rows:
        lines.append(f"## thread {row.get('thread_id', 'unknown')} (updated {row.get('updated_at', '')})")
        for message in row.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("type") or message.get("role") or "message")
            content = message.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            lines.append(f"- **{role}**: {text}")
        lines.append("")

    report.add("session_threads", len(rows))
    if report.dry_run:
        return

    target = space.inbox_dir / f"imported-sessions-{report.source_agent}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def import_bundle(
    path: str | Path,
    *,
    merge: str = MERGE_SKIP,
    dry_run: bool = False,
    rebind_chat: int | None = None,
    workspace: Workspace | None = None,
) -> ImportReport:
    """Import a bundle into the configured agent and return what happened."""
    if merge not in MERGE_MODES:
        raise BundleError(f"unknown merge mode '{merge}' (expected one of: {', '.join(MERGE_MODES)})")

    archive = Path(path)
    if not archive.exists():
        raise BundleError(f"bundle not found: {archive}")

    # Resolved at call time so a swapped kronos.workspace.ws is honoured.
    from kronos.workspace import ws

    space = workspace or ws

    with TemporaryDirectory(prefix="kaos-import-") as staging:
        root = Path(staging)
        _safe_extract(archive, root)

        manifest = read_manifest(root)
        problems = verify_manifest(root)
        if problems:
            raise BundleError("bundle failed verification: " + "; ".join(problems[:5]))

        report = ImportReport(
            source=archive,
            source_agent=manifest.agent_name,
            merge=merge,
            dry_run=dry_run,
            manifest=manifest,
        )

        _import_facts(root, report)
        _import_graph(root, report)
        _import_shared_facts(root, report)
        _import_skills(root, space, report)
        _import_persona(root, space, report)
        _import_schedule(root, report, rebind_chat=rebind_chat)
        if SECTION_NOTES in manifest.includes:
            _import_notes(root, space, report)
        if SECTION_SESSIONS in manifest.includes:
            _import_sessions(root, space, report)

    log.info(
        "Bundle import %s: %d new records from '%s'",
        "planned" if dry_run else "done",
        report.total_created,
        report.source_agent,
    )
    return report
