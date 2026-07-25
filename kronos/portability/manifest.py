"""Agent bundle manifest — the integrity contract of a `.kaos` bundle.

A bundle is a zip archive whose `manifest.json` lists every artifact with its
SHA-256. The manifest is what makes a bundle verifiable: an importer can prove
the payload is exactly what the exporter wrote before touching any database.

Determinism rule: `created_at` is the ONLY field carrying wall-clock time.
Everything else is derived from content, so exporting unchanged state twice
produces identical artifact hashes.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kronos import __version__

BUNDLE_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"

# Bundle sections. Persona/skills/memory are always present (possibly empty);
# notes/sessions are opt-in because they carry the most private content.
SECTION_PERSONA = "persona"
SECTION_SKILLS = "skills"
SECTION_FACTS = "facts"
SECTION_GRAPH = "graph"
SECTION_SHARED_FACTS = "shared_facts"
SECTION_SCHEDULE = "schedule"
SECTION_NOTES = "notes"
SECTION_SESSIONS = "sessions"

ALL_SECTIONS = (
    SECTION_PERSONA,
    SECTION_SKILLS,
    SECTION_FACTS,
    SECTION_GRAPH,
    SECTION_SHARED_FACTS,
    SECTION_SCHEDULE,
    SECTION_NOTES,
    SECTION_SESSIONS,
)

_HASH_PREFIX = "sha256:"
_READ_CHUNK = 65536


class BundleError(Exception):
    """Raised when a bundle is malformed, unreadable, or fails verification."""


@dataclass
class BundleManifest:
    """Describes a bundle's provenance and content hashes."""

    agent_name: str
    created_at: str
    schema_version: int = BUNDLE_SCHEMA_VERSION
    kaos_version: str = __version__
    includes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize canonically: sorted keys, stable indentation, UTF-8 text."""
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "BundleManifest":
        """Parse a manifest, rejecting anything that is not a v1-shaped object."""
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise BundleError(f"manifest is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise BundleError("manifest must be a JSON object")

        missing = [key for key in ("agent_name", "created_at", "schema_version") if key not in raw]
        if missing:
            raise BundleError(f"manifest is missing required fields: {', '.join(missing)}")

        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in raw.items() if key in known})

    def ensure_supported(self) -> None:
        """Fail loudly on a bundle from a newer, unknown schema.

        Older schemas stay readable — that is the point of versioning — but a
        future layout must not be silently half-imported.
        """
        if not isinstance(self.schema_version, int):
            raise BundleError(f"manifest schema_version must be an int, got {self.schema_version!r}")
        if self.schema_version > BUNDLE_SCHEMA_VERSION:
            raise BundleError(
                f"bundle schema v{self.schema_version} is newer than this KAOS supports "
                f"(v{BUNDLE_SCHEMA_VERSION}) — upgrade kronos-agent-os to import it"
            )


def file_sha256(path: Path) -> str:
    """Hash one file, streamed so large sessions/notes do not load into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return f"{_HASH_PREFIX}{digest.hexdigest()}"


def hash_artifacts(bundle_dir: Path) -> dict[str, str]:
    """Map every payload file to its hash, keyed by POSIX-relative path.

    The manifest itself is excluded (it holds the hashes) and keys are sorted so
    two exports of the same state yield byte-identical manifests.
    """
    artifacts: dict[str, str] = {}
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        artifacts[path.relative_to(bundle_dir).as_posix()] = file_sha256(path)
    return artifacts


def build_manifest(
    bundle_dir: Path,
    *,
    agent_name: str,
    includes: list[str],
    counts: dict[str, int],
    created_at: str = "",
) -> BundleManifest:
    """Build a manifest for an already-populated bundle directory."""
    unknown = [section for section in includes if section not in ALL_SECTIONS]
    if unknown:
        raise BundleError(f"unknown bundle sections: {', '.join(unknown)}")

    return BundleManifest(
        agent_name=agent_name,
        created_at=created_at or datetime.now(UTC).isoformat(timespec="seconds"),
        includes=sorted(includes),
        counts=dict(sorted(counts.items())),
        artifacts=hash_artifacts(bundle_dir),
    )


def write_manifest(bundle_dir: Path, manifest: BundleManifest) -> Path:
    """Write `manifest.json` into a bundle directory."""
    path = bundle_dir / MANIFEST_NAME
    path.write_text(manifest.to_json(), encoding="utf-8")
    return path


def read_manifest(bundle_dir: Path) -> BundleManifest:
    """Read and validate a bundle's manifest."""
    path = bundle_dir / MANIFEST_NAME
    if not path.exists():
        raise BundleError(f"{MANIFEST_NAME} not found in {bundle_dir}")
    manifest = BundleManifest.from_json(path.read_text(encoding="utf-8"))
    manifest.ensure_supported()
    return manifest


def verify_manifest(bundle_dir: Path) -> list[str]:
    """Return human-readable mismatches between manifest and payload.

    An empty list means the payload is exactly what the manifest claims.
    """
    manifest = read_manifest(bundle_dir)
    actual = hash_artifacts(bundle_dir)
    problems: list[str] = []

    for relpath, expected in sorted(manifest.artifacts.items()):
        if relpath not in actual:
            problems.append(f"missing file: {relpath}")
        elif actual[relpath] != expected:
            problems.append(f"hash mismatch: {relpath}")

    for relpath in sorted(set(actual) - set(manifest.artifacts)):
        problems.append(f"unlisted file: {relpath}")

    return problems
