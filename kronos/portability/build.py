"""Bundle construction primitives.

``export.py`` builds a bundle from this agent's databases; importers build one
from a foreign export (ChatGPT, Obsidian, Letta…). Both need the same staging,
canonical JSONL and deterministic zip, so those live here — the low level that
knows how to write a bundle, not where the content came from.
"""

import json
import logging
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp

from kronos.portability.manifest import (
    SECTION_FACTS,
    SECTION_GRAPH,
    SECTION_NOTES,
    SECTION_PERSONA,
    SECTION_SESSIONS,
    SECTION_SKILLS,
    BundleManifest,
    build_manifest,
    write_manifest,
)
from kronos.portability.redact import redact_structure, redact_text

log = logging.getLogger("kronos.portability.build")

# A fixed timestamp keeps the archive byte-identical across runs; the real
# creation time lives in the manifest, which is the one place time belongs.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_PERSONA_FILENAMES = ("IDENTITY.md", "SOUL.md", "AGENTS.md", "methodology.md")


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    """Write rows as canonical JSONL. Returns the number of rows written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    return written


def write_zip(source_dir: Path, out_path: Path) -> None:
    """Zip a staged bundle deterministically (sorted entries, fixed mtime)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(source_dir).as_posix(), date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


@dataclass
class BundleBuilder:
    """Accumulates bundle content in memory, then writes a `.kaos` archive.

    Importers convert a foreign export into a bundle rather than writing into
    KAOS databases directly. That way every import — from a KAOS peer or from
    ChatGPT — goes through the same verification, dedupe and dry-run path.
    """

    agent_name: str
    created_at: str = ""
    facts: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    sessions: list[dict] = field(default_factory=list)
    persona: dict[str, str] = field(default_factory=dict)
    skills: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add_fact(self, content: str, *, user_id: str, created_at: str = "", source: str = "import") -> bool:
        """Record a fact. Returns False when it is empty or a duplicate."""
        text = redact_text(" ".join(content.split()))
        if len(text) < 3:
            return False
        if any(existing["content"] == text and existing["user_id"] == user_id for existing in self.facts):
            return False
        self.facts.append({"user_id": user_id, "content": text, "created_at": created_at, "source": source})
        return True

    def add_entity(self, name: str, entity_type: str, properties: dict | None = None) -> None:
        name, entity_type = name.strip(), entity_type.strip()
        if not name or not entity_type:
            return
        if any(e["name"] == name and e["type"] == entity_type for e in self.entities):
            return
        self.entities.append({"name": name, "type": entity_type, "properties": properties or {}})

    def add_relation(
        self,
        source_name: str,
        source_type: str,
        target_name: str,
        target_type: str,
        relation_type: str,
    ) -> None:
        if not (source_name.strip() and target_name.strip() and relation_type.strip()):
            return
        self.add_entity(source_name, source_type)
        self.add_entity(target_name, target_type)
        row = {
            "source": {"name": source_name.strip(), "type": source_type},
            "target": {"name": target_name.strip(), "type": target_type},
            "relation_type": relation_type.strip(),
            "properties": {},
        }
        if row not in self.relations:
            self.relations.append(row)

    def add_note(self, relpath: str, content: str) -> None:
        """Add a markdown note under notes/<relpath>."""
        safe = relpath.strip("/").replace("..", "_")
        if not safe.endswith(".md"):
            safe = f"{safe}.md"
        self.notes[safe] = redact_text(content)

    def add_session(self, thread_id: str, messages: list[dict], *, updated_at: str = "") -> None:
        """Add one conversation thread (masked: this is third-party content)."""
        if not messages:
            return
        self.sessions.append(
            {
                "thread_id": str(thread_id),
                "updated_at": updated_at,
                "messages": redact_structure(messages, mask_personal=True),
            }
        )

    def add_persona(self, filename: str, content: str) -> None:
        """Add a persona file. Only the four canonical names are accepted."""
        if filename not in _PERSONA_FILENAMES:
            self.warnings.append(f"ignored unknown persona file: {filename}")
            return
        self.persona[filename] = redact_text(content)

    def add_skill(self, name: str, skill_md: str, references: dict[str, str] | None = None) -> None:
        self.skills[name] = {
            "SKILL.md": redact_text(skill_md),
            "references": {ref: redact_text(body) for ref, body in (references or {}).items()},
        }

    def _includes(self) -> list[str]:
        includes: list[str] = []
        if self.persona:
            includes.append(SECTION_PERSONA)
        if self.skills:
            includes.append(SECTION_SKILLS)
        if self.facts:
            includes.append(SECTION_FACTS)
        if self.entities or self.relations:
            includes.append(SECTION_GRAPH)
        if self.notes:
            includes.append(SECTION_NOTES)
        if self.sessions:
            includes.append(SECTION_SESSIONS)
        return includes

    def counts(self) -> dict[str, int]:
        return {
            "facts": len(self.facts),
            "graph_entities": len(self.entities),
            "graph_relations": len(self.relations),
            "notes": len(self.notes),
            "persona_files": len(self.persona),
            "sessions": len(self.sessions),
            "skills": len(self.skills),
        }

    def is_empty(self) -> bool:
        return not any(self.counts().values())

    def write(self, out_path: str | Path) -> tuple[Path, BundleManifest]:
        """Stage the accumulated content and write the archive."""
        target = Path(out_path)
        staging = Path(mkdtemp(prefix="kaos-build-"))
        try:
            for filename, content in sorted(self.persona.items()):
                path = staging / "persona" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            for name, payload in sorted(self.skills.items()):
                skill_path = staging / "skills" / name / "SKILL.md"
                skill_path.parent.mkdir(parents=True, exist_ok=True)
                skill_path.write_text(payload["SKILL.md"], encoding="utf-8")
                for ref, body in sorted(payload["references"].items()):
                    ref_path = staging / "skills" / name / "references" / ref
                    ref_path.parent.mkdir(parents=True, exist_ok=True)
                    ref_path.write_text(body, encoding="utf-8")

            if self.facts:
                write_jsonl(staging / "memory" / "facts.jsonl", sorted(self.facts, key=_fact_sort_key))
            if self.entities or self.relations:
                write_jsonl(
                    staging / "memory" / "graph.entities.jsonl",
                    sorted(self.entities, key=lambda row: (row["type"], row["name"])),
                )
                write_jsonl(
                    staging / "memory" / "graph.relations.jsonl",
                    sorted(self.relations, key=_relation_sort_key),
                )
            for relpath, content in sorted(self.notes.items()):
                note_path = staging / "notes" / relpath
                note_path.parent.mkdir(parents=True, exist_ok=True)
                note_path.write_text(content, encoding="utf-8")
            if self.sessions:
                write_jsonl(
                    staging / "sessions" / "sessions.jsonl",
                    sorted(self.sessions, key=lambda row: row["thread_id"]),
                )

            manifest = build_manifest(
                staging,
                agent_name=self.agent_name,
                includes=self._includes(),
                counts=self.counts(),
                created_at=self.created_at,
            )
            write_manifest(staging, manifest)
            write_zip(staging, target)
        finally:
            for path in sorted(staging.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            staging.rmdir()

        log.info("Built bundle %s from '%s': %s", target, self.agent_name, self.counts())
        return target, manifest


def _fact_sort_key(row: dict) -> tuple:
    return (str(row.get("created_at", "")), row.get("user_id", ""), row.get("content", ""))


def _relation_sort_key(row: dict) -> tuple:
    return (
        row["source"]["type"],
        row["source"]["name"],
        row["relation_type"],
        row["target"]["type"],
        row["target"]["name"],
    )
