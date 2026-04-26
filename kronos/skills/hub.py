"""Skills Hub — import/export skills following agentskills.io standard.

Supports:
- Import from URL or github:user/repo/skill-name
- Export skill as standalone package
- Manifest generation
"""

import logging
import re
import urllib.request

from kronos.skills.store import SkillStore, _parse_frontmatter

log = logging.getLogger("kronos.skills.hub")

GITHUB_RAW_URL = "https://raw.githubusercontent.com/{user}/{repo}/main/{path}/SKILL.md"


def _fetch_url(url: str, timeout: int = 15) -> str:
    """Fetch content from URL.

    Args:
        url: Target URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        Decoded response body as string.

    Raises:
        urllib.error.URLError: On network errors.
        urllib.error.HTTPError: On non-2xx HTTP responses.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "kronos-ii/1.0"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode("utf-8")


def _parse_github_source(source: str) -> str | None:
    """Parse github:user/repo/skill-name into raw GitHub content URL.

    Args:
        source: Source string in 'github:user/repo/skill-name' format.

    Returns:
        Raw GitHub URL or None if format does not match.
    """
    match = re.match(r"github:([\w.-]+)/([\w.-]+)/([\w.-]+)", source)
    if not match:
        return None
    user, repo, skill = match.groups()
    return GITHUB_RAW_URL.format(user=user, repo=repo, path=skill)


def import_skill(source: str, store: SkillStore) -> str:
    """Import a skill from URL or github:user/repo/skill-name.

    Fetches the remote SKILL.md, validates its frontmatter, checks for
    name conflicts, and registers the skill via the store.

    Args:
        source: URL to SKILL.md or 'github:user/repo/skill-name'.
        store: SkillStore instance to register the imported skill into.

    Returns:
        Human-readable status message describing the outcome.
    """
    # Resolve source to a concrete URL
    url = _parse_github_source(source)
    if not url:
        if source.startswith("http"):
            url = source
        else:
            return (
                f"Invalid source: '{source}'. "
                "Use a URL or 'github:user/repo/skill-name' format."
            )

    # Fetch SKILL.md content
    try:
        content = _fetch_url(url)
    except Exception as e:
        return f"Failed to fetch skill from '{url}': {e}"

    if not content.strip():
        return "Empty skill content received."

    # Parse and validate frontmatter
    meta, body = _parse_frontmatter(content)

    name = meta.get("name", "").strip()
    if not name:
        return "Skill has no 'name' in frontmatter."

    description = meta.get("description", "").strip()
    if not description:
        return "Skill has no 'description' in frontmatter."

    # Guard against name collisions
    existing = store.get(name)
    if existing:
        return (
            f"Skill '{name}' already exists. Remove it first to re-import."
        )

    # Log required tools for informational purposes (no enforcement yet)
    tools_raw = meta.get("tools", "")
    required_tools = [t.strip() for t in tools_raw.strip("[]").split(",") if t.strip()]
    if required_tools:
        log.info("Imported skill '%s' requires tools: %s", name, required_tools)

    # Persist and register
    store.add_skill(name, body, meta)

    version = meta.get("version", "unknown")
    author = meta.get("author", "unknown")
    tools_display = ", ".join(required_tools) if required_tools else "none"

    return (
        f"Skill '{name}' v{version} imported successfully "
        f"(author: {author}, tools: {tools_display})"
    )


def export_skill(name: str, store: SkillStore) -> str | None:
    """Export a skill as its full SKILL.md content.

    Args:
        name: Skill name to export.
        store: SkillStore instance to look up the skill in.

    Returns:
        Full SKILL.md file content as string, or None if skill not found.
    """
    skill = store.get(name)
    if not skill:
        return None
    return skill.path.read_text(encoding="utf-8")
