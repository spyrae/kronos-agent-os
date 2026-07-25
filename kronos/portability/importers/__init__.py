"""Importers that convert a foreign export into a `.kaos` bundle.

Every importer produces a bundle rather than writing into KAOS databases, so a
history coming from ChatGPT, Obsidian or Letta goes through exactly the same
verification, dedupe, merge and dry-run path as a bundle from a KAOS peer.

Contract per importer module:

    NAME: str
    def detect(path: Path) -> bool
    def to_bundle(path: Path, out_path: Path, *, limit: int | None = None) -> ImporterResult
"""

from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from kronos.portability.importers import chatgpt, claude_projects, letta, obsidian, telegram
from kronos.portability.manifest import BundleError, BundleManifest


@dataclass
class ImporterResult:
    """A bundle produced from a foreign export."""

    importer: str
    bundle: Path
    manifest: BundleManifest
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Converted {self.importer} export → {self.bundle.name}"]
        for key, value in sorted(self.counts.items()):
            if value:
                lines.append(f"  {key}: {value}")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)


# Detection order matters: claude-projects and letta identify themselves by a
# specific marker file, while obsidian accepts any markdown directory, so the
# strict ones must be probed first. `available()` keeps that order stable.
_REGISTRY: dict[str, ModuleType] = {
    chatgpt.NAME: chatgpt,
    claude_projects.NAME: claude_projects,
    letta.NAME: letta,
    telegram.NAME: telegram,
    obsidian.NAME: obsidian,
}


def available() -> list[str]:
    """Importer names in detection order (strict matchers first)."""
    return list(_REGISTRY)


def get_importer(name: str) -> ModuleType:
    """Look up an importer by name."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise BundleError(f"unknown importer '{name}' (available: {', '.join(available())})") from None


def detect_importer(path: str | Path) -> str | None:
    """Guess which importer can read this path, or None if none recognise it."""
    target = Path(path)
    for name in available():
        if _REGISTRY[name].detect(target):
            return name
    return None


__all__ = ["ImporterResult", "available", "detect_importer", "get_importer"]
