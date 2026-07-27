"""Skill registry: named sources, a cached index, and install with proof.

This is a file, not a service. `registry.yaml` lists where skills may come from
and how much each source must prove; the index is JSON fetched from those sources
and cached locally so `search` works offline afterwards. Nobody has to run
infrastructure for this to be useful, and no source is trusted by being reachable.

Install reuses the existing import path (`skills.hub.import_skill`) rather than a
second downloader, so the egress allowlist, the name-collision guard and the
draft-by-default rule all still apply. What this adds on top is the verification
step and the decision that follows from it:

* a `signed` source whose signature verifies against a configured key installs
  **active** — a key you configured vouching for the exact bytes is stronger
  evidence than a human skimming markdown;
* everything else installs as a **draft** with the reason attached, which is the
  pre-existing behaviour for external skills.

A failure never deletes anything. The worst case is a draft you can read.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from kronos.skills.store import SkillStore

log = logging.getLogger("kronos.skills.registry")

DEFAULT_REGISTRY_FILE = "registry.yaml"
ENV_REGISTRY_FILE = "KAOS_REGISTRY_FILE"
REGISTRY_SCHEMA_VERSION = 1

# Convention for a github source: the index lives at the repository root.
INDEX_FILENAME = "index.json"

# How long a cached index is served without refetching.
CACHE_TTL_SECONDS = 6 * 3600

TRUST_LEVELS = ("signed", "checksum", "none")


class RegistryError(Exception):
    """Raised when registry.yaml or a source index cannot be used."""


@dataclass(frozen=True)
class RegistrySource:
    name: str
    url: str
    trust: str = "checksum"

    def __post_init__(self) -> None:
        if not self.name:
            raise RegistryError("a registry source needs a name")
        if not self.url:
            raise RegistryError(f"source '{self.name}' has no url")
        if self.trust not in TRUST_LEVELS:
            raise RegistryError(f"source '{self.name}' has unknown trust '{self.trust}' (expected {TRUST_LEVELS})")


@dataclass
class RegistryEntry:
    """One skill as advertised by a source. Claims, not facts, until verified."""

    name: str
    description: str = ""
    version: str = ""
    url: str = ""
    author: str = ""
    requires_kaos: str = ""
    checksum: str = ""
    signed: bool = False
    source: str = ""
    trust: str = "checksum"

    def matches(self, query: str) -> bool:
        needle = query.strip().lower()
        if not needle:
            return True
        return needle in self.name.lower() or needle in self.description.lower()


@dataclass
class InstallResult:
    """What happened, in enough detail to explain a refusal."""

    skill: str
    installed: bool
    status: str = "failed"  # active | draft | failed
    reason: str = ""
    report: dict = field(default_factory=dict)

    def render(self) -> str:
        if not self.installed:
            return f"[FAIL] {self.skill}: {self.reason}"
        headline = f"[{'ACTIVE' if self.status == 'active' else 'DRAFT'}] {self.skill} installed"
        return f"{headline}: {self.reason}" if self.reason else headline


# ----------------------------------------------------------------------------
# registry.yaml
# ----------------------------------------------------------------------------


def registry_file_path(path: str | Path | None = None) -> Path:
    import os

    if path is not None:
        return Path(path)
    from_env = os.environ.get(ENV_REGISTRY_FILE)
    if from_env:
        return Path(from_env)
    return (Path(__file__).resolve().parents[2] / DEFAULT_REGISTRY_FILE).resolve()


def load_sources(path: str | Path | None = None) -> list[RegistrySource]:
    """Read the configured sources. No file means no sources, not an error."""
    registry_path = registry_file_path(path)
    if not registry_path.exists():
        return []

    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise RegistryError(f"{registry_path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise RegistryError(f"{registry_path} must be a mapping with a 'sources' list")

    version = int(raw.get("version", REGISTRY_SCHEMA_VERSION) or REGISTRY_SCHEMA_VERSION)
    if version > REGISTRY_SCHEMA_VERSION:
        raise RegistryError(f"registry schema v{version} is newer than this KAOS supports (v{REGISTRY_SCHEMA_VERSION})")

    entries = raw.get("sources") or []
    if not isinstance(entries, list):
        raise RegistryError(f"{registry_path}: 'sources' must be a list")

    from kronos.policy import get_policy

    try:
        default_trust = get_policy().registry.trust_default
    except Exception:  # pragma: no cover - policy is optional
        default_trust = "checksum"

    sources: list[RegistrySource] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryError(f"{registry_path}: each source must be a mapping, got {type(entry).__name__}")
        source = RegistrySource(
            name=str(entry.get("name", "")).strip(),
            url=str(entry.get("url", "")).strip(),
            trust=str(entry.get("trust", default_trust)).strip(),
        )
        if source.name in seen:
            raise RegistryError(f"{registry_path}: duplicate source name '{source.name}'")
        seen.add(source.name)
        sources.append(source)
    return sources


# ----------------------------------------------------------------------------
# index
# ----------------------------------------------------------------------------


def index_url(source: RegistrySource) -> str:
    """Where a source's index lives."""
    if source.url.startswith("github:"):
        from kronos.skills.hub import GITHUB_RAW_URL

        parts = source.url.removeprefix("github:").strip("/").split("/")
        if len(parts) < 2:
            raise RegistryError(f"source '{source.name}': expected github:user/repo, got '{source.url}'")
        user, repo = parts[0], parts[1]
        prefix = "/".join(parts[2:])
        path = f"{prefix}/{INDEX_FILENAME}" if prefix else INDEX_FILENAME
        return GITHUB_RAW_URL.format(user=user, repo=repo, path=path)
    if source.url.startswith(("http://", "https://")):
        return source.url
    raise RegistryError(f"source '{source.name}': url must be github:user/repo or an http(s) URL")


def parse_index(source: RegistrySource, payload: str) -> list[RegistryEntry]:
    """Turn a source's index JSON into entries."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise RegistryError(f"source '{source.name}' returned invalid JSON: {e}") from e

    skills = data.get("skills") if isinstance(data, dict) else data
    if not isinstance(skills, list):
        raise RegistryError(f"source '{source.name}': index must contain a 'skills' list")

    entries: list[RegistryEntry] = []
    for item in skills:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            log.warning("Source '%s' advertises an entry without a name; skipping it", source.name)
            continue
        entries.append(
            RegistryEntry(
                name=str(item["name"]).strip(),
                description=str(item.get("description", "")).strip(),
                version=str(item.get("version", "")).strip(),
                url=str(item.get("url") or item.get("source") or "").strip(),
                author=str(item.get("author", "")).strip(),
                requires_kaos=str(item.get("requires_kaos", "")).strip(),
                checksum=str(item.get("checksum", "")).strip(),
                signed=bool(item.get("signed", False)),
                source=source.name,
                trust=source.trust,
            )
        )
    return entries


def _cache_path() -> Path:
    from kronos.config import settings

    return Path(settings.db_dir) / "registry-cache.json"


def _read_cache() -> dict:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.debug("Registry cache unreadable (%s); refetching", e)
        return {}


def _write_cache(entries: list[RegistryEntry], *, now: float) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": now, "skills": [asdict(entry) for entry in entries]}, indent=2),
            encoding="utf-8",
        )
    except OSError as e:  # pragma: no cover - cache is best-effort
        log.debug("Could not write the registry cache: %s", e)


def load_index(
    sources: list[RegistrySource] | None = None,
    *,
    refresh: bool = False,
    now: float | None = None,
) -> tuple[list[RegistryEntry], list[str]]:
    """Entries from every source, plus one message per source that failed.

    A source being down is reported, never raised: one unreachable host must not
    make `search` useless for the others.
    """
    sources = load_sources() if sources is None else sources
    now = time.time() if now is None else now

    if not refresh:
        cached = _read_cache()
        fetched_at = float(cached.get("fetched_at", 0) or 0)
        if cached.get("skills") and now - fetched_at < CACHE_TTL_SECONDS:
            return [RegistryEntry(**row) for row in cached["skills"]], []

    from kronos.skills.hub import _fetch_url

    entries: list[RegistryEntry] = []
    problems: list[str] = []
    for source in sources:
        try:
            payload = _fetch_url(index_url(source))
            entries.extend(parse_index(source, payload))
        except RegistryError as e:
            problems.append(str(e))
        except Exception as e:
            problems.append(f"source '{source.name}' is unreachable: {e}")

    if entries:
        _write_cache(entries, now=now)
    return entries, problems


def search(query: str, entries: list[RegistryEntry] | None = None) -> list[RegistryEntry]:
    if entries is None:
        entries, _ = load_index()
    return sorted((entry for entry in entries if entry.matches(query)), key=lambda entry: (entry.source, entry.name))


def find_entry(name: str, entries: list[RegistryEntry], *, source: str = "") -> RegistryEntry | None:
    """Exact match by name, optionally pinned to one source."""
    candidates = [entry for entry in entries if entry.name == name and (not source or entry.source == source)]
    return candidates[0] if candidates else None


# ----------------------------------------------------------------------------
# install
# ----------------------------------------------------------------------------


def install(
    name: str,
    *,
    store: SkillStore,
    source: str = "",
    entries: list[RegistryEntry] | None = None,
    allow_activate: bool = True,
) -> InstallResult:
    """Install one advertised skill, then decide what its proof allows.

    The skill lands as a draft first (the existing import path), and is promoted
    only when the source demands a signature and that signature verifies.
    """
    if entries is None:
        entries, problems = load_index()
        if not entries:
            detail = problems[0] if problems else "no sources configured in registry.yaml"
            return InstallResult(skill=name, installed=False, reason=detail)

    entry = find_entry(name, entries, source=source)
    if entry is None:
        where = f" in source '{source}'" if source else ""
        return InstallResult(skill=name, installed=False, reason=f"'{name}' is not in the index{where}")
    if not entry.url:
        return InstallResult(skill=name, installed=False, reason=f"'{name}' has no url in the index")

    from kronos.skills.hub import import_skill

    message = import_skill(entry.url, store)
    if "imported successfully" not in message:
        return InstallResult(skill=name, installed=False, reason=message)

    return finish_install(entry, store=store, allow_activate=allow_activate)


def finish_install(
    entry: RegistryEntry,
    *,
    store: SkillStore,
    allow_activate: bool = True,
    eval_detail: str = "",
) -> InstallResult:
    """Verify a freshly imported skill and set its status accordingly."""
    from kronos.skills.integrity import trusted_keys, verify_skill

    skill = store.get(entry.name)
    if skill is None:  # pragma: no cover - import reported success
        return InstallResult(skill=entry.name, installed=False, reason="skill vanished after import")

    report = verify_skill(skill, keys=trusted_keys())
    report["source"] = entry.source
    report["trust"] = entry.trust

    reasons: list[str] = []
    if entry.checksum and skill.checksum and entry.checksum.strip() != skill.checksum.strip():
        reasons.append("the index advertises a different checksum than the skill declares")
    if not report["compatible"]:
        reasons.append(report["compatibility_detail"])

    # Checksum first, then signature — a failed checksum already explains a failed
    # signature (the signature covers it), and saying it twice reads like two
    # separate problems.
    checksum_matters = entry.trust in ("signed", "checksum")
    if checksum_matters and report["unverified"]:
        reasons.append("skill declares no checksum, so nothing can be verified")
    elif checksum_matters and not report["checksum_ok"]:
        reasons.append(report["checksum_detail"])
    elif entry.trust == "signed" and not report["signature_ok"]:
        reasons.append(f"source requires a signature: {report['signature_detail']}")

    # Proof strong enough to skip a human read: the source demands signing and a
    # key we configured signed these exact bytes.
    proven = report["signature_ok"] and report["checksum_ok"] and report["compatible"] and entry.trust == "signed"
    status = "active" if (proven and allow_activate and not reasons) else "draft"
    if status == "active":
        store.update_status(entry.name, "active")

    reason = "; ".join(reasons)
    if not reason:
        reason = report["signature_detail"] if proven else "installed for review (no signature to vouch for it)"
    if eval_detail:
        reason = f"{reason}; {eval_detail}"

    return InstallResult(skill=entry.name, installed=True, status=status, reason=reason, report=report)
