"""Import an Obsidian vault.

A vault is already the shape KAOS calls notes, so the mapping is direct:

* every markdown file becomes a note under the same relative path;
* `[[wikilinks]]` become graph relations, which is the part a plain file copy
  would throw away — the link structure is the vault's actual knowledge;
* notes marked `type: fact` (or listing `facts:` in frontmatter) become
  extracted facts, so they participate in recall instead of only sitting on disk.
"""

import logging
import re
from pathlib import Path

from kronos.portability.build import BundleBuilder
from kronos.portability.manifest import BundleError
from kronos.skills.store import _parse_frontmatter, _parse_list_field

log = logging.getLogger("kronos.portability.importers.obsidian")

NAME = "obsidian"

_SKIP_DIRS = frozenset({".obsidian", ".trash", ".git", "node_modules", "__pycache__"})
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_MAX_NOTE_BYTES = 1024 * 1024
_MAX_FACT_CHARS = 400


def detect(path: Path) -> bool:
    """True for a directory that looks like a vault (has .obsidian or markdown)."""
    if not path.is_dir():
        return False
    if (path / ".obsidian").exists():
        return True
    return any(True for _ in _markdown_files(path))


def _markdown_files(root: Path):
    for candidate in sorted(root.rglob("*.md")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        yield candidate


def to_bundle(
    path: str | Path,
    out_path: str | Path,
    *,
    limit: int | None = None,
    user_id: str = "",
    created_at: str = "",
):
    """Convert an Obsidian vault into a bundle at ``out_path``."""
    from kronos.config import settings
    from kronos.portability.importers import ImporterResult

    root = Path(path)
    if not root.is_dir():
        raise BundleError(f"not a vault directory: {root}")

    files = list(_markdown_files(root))
    dropped = 0
    if limit is not None and len(files) > limit:
        dropped = len(files) - limit
        files = files[:limit]

    builder = BundleBuilder(agent_name=NAME, created_at=created_at)
    owner = user_id or settings.agent_name

    for note_path in files:
        if note_path.stat().st_size > _MAX_NOTE_BYTES:
            builder.warnings.append(f"skipped oversized note: {note_path.name}")
            continue
        try:
            raw = note_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            builder.warnings.append(f"skipped non-text note: {note_path.name}")
            continue

        relative = note_path.relative_to(root)
        builder.add_note(f"world/vault/{relative.as_posix()}", raw)

        meta, body = _parse_frontmatter(raw)
        title = note_path.stem

        for target in {match.strip() for match in _WIKILINK_RE.findall(raw) if match.strip()}:
            builder.add_relation(title, "note", target, "note", "links_to")

        if str(meta.get("type", "")).strip().lower() == "fact":
            fact = " ".join(body.split())[:_MAX_FACT_CHARS]
            builder.add_fact(fact, user_id=owner, created_at=str(meta.get("date", "")), source="obsidian")

        for fact in _parse_list_field(meta.get("facts", "")):
            builder.add_fact(fact[:_MAX_FACT_CHARS], user_id=owner, created_at="", source="obsidian")

    if builder.is_empty():
        raise BundleError(f"no importable markdown found in {root}")

    if dropped:
        builder.warnings.append(f"limited to {limit} notes, {dropped} skipped")

    bundle, manifest = builder.write(out_path)
    log.info(
        "Obsidian import: %d notes, %d facts, %d links", len(builder.notes), len(builder.facts), len(builder.relations)
    )
    return ImporterResult(
        importer=NAME,
        bundle=bundle,
        manifest=manifest,
        counts=builder.counts(),
        warnings=builder.warnings,
    )
