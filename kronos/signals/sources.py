"""Declarative source registry for Signal Intelligence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SignalSourceConfigError(ValueError):
    """Raised when the Signal Intelligence source registry is invalid."""


ALLOWED_PLATFORMS = {
    "analytics",
    "app_store",
    "competitor",
    "play_store",
    "reddit",
    "rss",
    "search",
    "seo",
    "telegram",
    "x",
}
ALLOWED_CATEGORIES = {
    "news",
    "jobs",
    "ideas",
    "travel_insights",
    "jb_competitors",
    "jb_system",
}
ALLOWED_TIERS = {"core", "candidate", "quarantine"}
ALLOWED_TRUST = {
    "official",
    "expert",
    "community_high",
    "community_low",
    "noisy",
}
CATEGORY_TITLES = {
    "news": "Digest: News",
    "jobs": "Digest: Jobs",
    "ideas": "Digest: Product/Business Ideas",
    "travel_insights": "JB: Travel Insights",
    "jb_competitors": "JB: Competitors Status",
    "jb_system": "JB: System Status",
}
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class SignalSource:
    """One normalized source used by the Signal Intelligence pipeline."""

    id: str
    platform: str
    categories: tuple[str, ...]
    tier: str
    trust: str
    language: str = "en"
    enabled: bool = True
    handle: str = ""
    url: str = ""
    query: str = ""
    description: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    @property
    def locator(self) -> str:
        """Return the primary human-readable location for the source."""
        return self.handle or self.url or self.query


@dataclass(frozen=True)
class SourceRegistry:
    """Validated collection of Signal Intelligence sources."""

    sources: tuple[SignalSource, ...]

    def get(self, source_id: str) -> SignalSource | None:
        """Return a source by id, if present."""
        return next((source for source in self.sources if source.id == source_id), None)

    def active(
        self,
        *,
        categories: set[str] | tuple[str, ...] | list[str] | None = None,
        platforms: set[str] | tuple[str, ...] | list[str] | None = None,
        include_quarantine: bool = False,
    ) -> list[SignalSource]:
        """Return enabled sources, excluding quarantine by default."""
        category_filter = {category.lower() for category in categories or ()}
        platform_filter = {_normalize_platform(platform) for platform in platforms or ()}
        result: list[SignalSource] = []

        for source in self.sources:
            if not source.enabled:
                continue
            if not include_quarantine and source.tier == "quarantine":
                continue
            if category_filter and not category_filter.intersection(source.categories):
                continue
            if platform_filter and source.platform not in platform_filter:
                continue
            result.append(source)

        return result

    def disabled(self) -> list[SignalSource]:
        """Return explicitly disabled sources."""
        return [source for source in self.sources if not source.enabled]

    def quarantined(self) -> list[SignalSource]:
        """Return sources kept for review but excluded from active collection."""
        return [source for source in self.sources if source.tier == "quarantine"]

    def news_monitor_queries(self, *, category: str = "news", limit: int | None = None) -> list[str]:
        """Render active Reddit/X/search sources as Brave-compatible queries."""
        queries: list[str] = []
        for source in self.active(categories=(category,), platforms=("reddit", "x", "search")):
            if source.platform == "search":
                if source.query:
                    queries.append(source.query)
                continue
            if source.platform == "reddit":
                context = source.description or source.handle
                queries.append(f"site:reddit.com {source.handle} {context}".strip())
                continue
            if source.platform == "x":
                context = source.description or source.handle
                queries.append(f"{context} {source.handle} news".strip())

        return queries[:limit] if limit is not None else queries

    def telegram_groups(
        self,
        *,
        categories: set[str] | tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        """Render active Telegram sources in the legacy group-digest shape."""
        requested = {category.lower() for category in categories or ()}
        groups: dict[str, list[dict[str, str]]] = {}

        for source in self.active(platforms=("telegram",)):
            source_categories = requested.intersection(source.categories) if requested else set(source.categories)
            for category in source_categories:
                title = CATEGORY_TITLES.get(category, category)
                groups.setdefault(title, []).append(
                    {
                        "name": source.description or source.id,
                        "identifier": source.handle,
                        "description": source.description,
                    }
                )

        return {category: values for category, values in groups.items() if values}


def default_sources_path() -> Path:
    """Return the workspace path for the Signal Intelligence source registry."""
    from kronos.workspace import ws

    return ws.skills_dir / "signal-intel" / "references" / "SOURCES.yaml"


def load_sources(path: str | Path | None = None) -> SourceRegistry:
    """Load and validate the Signal Intelligence source registry."""
    yaml_path = Path(path) if path is not None else default_sources_path()
    if not yaml_path.exists():
        raise SignalSourceConfigError(f"source registry not found: {yaml_path}")

    import yaml

    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SignalSourceConfigError(f"invalid YAML in {yaml_path}: {exc}") from exc

    return parse_sources(raw, source_name=str(yaml_path))


def parse_sources(raw: object, *, source_name: str = "SOURCES.yaml") -> SourceRegistry:
    """Validate a decoded source registry mapping."""
    root = _require_mapping(raw, source_name)
    entries = root.get("sources")
    if not isinstance(entries, list):
        raise SignalSourceConfigError(f"{source_name}: 'sources' must be a list")

    seen_ids: set[str] = set()
    sources: list[SignalSource] = []
    for index, entry in enumerate(entries):
        source = _parse_source(entry, index=index, source_name=source_name)
        if source.id in seen_ids:
            raise SignalSourceConfigError(f"{source_name}: duplicate source id '{source.id}'")
        seen_ids.add(source.id)
        sources.append(source)

    return SourceRegistry(tuple(sources))


def _parse_source(entry: object, *, index: int, source_name: str) -> SignalSource:
    context = f"{source_name}: sources[{index}]"
    data = _require_mapping(entry, context)

    source_id = _require_string(data, "id", context)
    if not _SOURCE_ID_RE.match(source_id):
        raise SignalSourceConfigError(
            f"{context}.id must match {_SOURCE_ID_RE.pattern!r}; got {source_id!r}"
        )

    platform = _normalize_platform(_require_string(data, "platform", context))
    _require_allowed(platform, ALLOWED_PLATFORMS, f"{context}.platform")

    categories = tuple(_require_string_list(data, "categories", context))
    if not categories:
        raise SignalSourceConfigError(f"{context}.categories must not be empty")
    for category in categories:
        _require_allowed(category, ALLOWED_CATEGORIES, f"{context}.categories")

    tier = _optional_string(data, "tier", default="candidate").lower()
    _require_allowed(tier, ALLOWED_TIERS, f"{context}.tier")

    trust = _optional_string(data, "trust", default="community_low").lower()
    _require_allowed(trust, ALLOWED_TRUST, f"{context}.trust")

    handle = _optional_string(data, "handle")
    url = _optional_string(data, "url")
    query = _optional_string(data, "query")
    if not (handle or url or query):
        raise SignalSourceConfigError(f"{context} must define one of handle, url, or query")

    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        raise SignalSourceConfigError(f"{context}.filters must be a mapping")

    tags = tuple(_optional_string_list(data, "tags"))

    return SignalSource(
        id=source_id,
        platform=platform,
        categories=categories,
        tier=tier,
        trust=trust,
        language=_optional_string(data, "language", default="en").lower(),
        enabled=_optional_bool(data, "enabled", default=True),
        handle=handle,
        url=url,
        query=query,
        description=_optional_string(data, "description"),
        filters=dict(filters),
        tags=tags,
    )


def _normalize_platform(platform: str) -> str:
    normalized = platform.strip().lower()
    return "x" if normalized in {"twitter", "x.com"} else normalized


def _require_mapping(raw: object, context: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SignalSourceConfigError(f"{context} must be a mapping")
    return raw


def _require_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SignalSourceConfigError(f"{context}.{key} is required and must be a string")
    return value.strip()


def _optional_string(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SignalSourceConfigError(f"{key} must be a string")
    return value.strip()


def _require_string_list(data: dict[str, Any], key: str, context: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise SignalSourceConfigError(f"{context}.{key} is required and must be a list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise SignalSourceConfigError(f"{context}.{key} must contain only strings")
    return [item.strip().lower() for item in value]


def _optional_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise SignalSourceConfigError(f"{key} must be a list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise SignalSourceConfigError(f"{key} must contain only strings")
    return [item.strip().lower() for item in value]


def _optional_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SignalSourceConfigError(f"{key} must be a boolean")
    return value


def _require_allowed(value: str, allowed: set[str], field_name: str) -> None:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise SignalSourceConfigError(f"{field_name} has unsupported value {value!r}; allowed: {allowed_values}")
