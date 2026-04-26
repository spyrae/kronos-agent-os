"""Load competitor configuration from YAML."""

import logging
from pathlib import Path

from kronos.competitors.models import CompetitorConfig

log = logging.getLogger("kronos.competitors.config")

_YAML_PATH = Path(__file__).parent / "competitors.yaml"


def load_competitors(path: Path | None = None) -> list[CompetitorConfig]:
    """Load competitor list from YAML config."""
    yaml_path = path or _YAML_PATH
    if not yaml_path.exists():
        log.warning("Competitors config not found: %s", yaml_path)
        return []

    import yaml

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    competitors = []
    for entry in raw.get("competitors", []):
        competitors.append(CompetitorConfig(
            id=entry["id"],
            name=entry["name"],
            tier=entry.get("tier", 2),
            ios_id=entry.get("ios_id", ""),
            android_package=entry.get("android_package", ""),
            website=entry.get("website", ""),
            blog_rss=entry.get("blog_rss", ""),
            twitter=entry.get("twitter", ""),
            linkedin=entry.get("linkedin", ""),
        ))

    log.info("Loaded %d competitors (%d tier-1)", len(competitors),
             sum(1 for c in competitors if c.tier == 1))
    return competitors
