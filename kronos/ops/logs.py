"""Resolve runtime log locations for ops readers.

The runtime writer stores logs next to the active session database:
``Path(settings.db_path).parent / "logs"``. Ops readers mirror that contract
without creating directories.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LogSource:
    """One resolved log source directory."""

    label: str
    path: Path
    reason: str


@dataclass(frozen=True)
class LogResolution:
    """Resolved log topology for ops reports."""

    mode: str
    sources: tuple[LogSource, ...]
    warnings: tuple[str, ...] = ()

    def jsonl_paths(self, filename: str, *, existing_only: bool = False) -> tuple[Path, ...]:
        """Return candidate JSONL paths for all resolved sources."""
        paths = tuple(source.path / filename for source in self.sources)
        if existing_only:
            return tuple(path for path in paths if path.is_file())
        return paths


def _default_app_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _abs_path(value: str, app_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = app_dir / path
    return path.resolve()


def resolve_log_sources(
    *,
    app_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LogResolution:
    """Resolve log source directories using the KAOS ops topology.

    Precedence:
    1. ``KAOS_LOG_DIR`` explicit single-source override.
    2. Aggregate mode via ``KAOS_LOG_MODE=aggregate`` or agent name ``all``.
    3. ``DB_PATH`` -> ``dirname(DB_PATH)/logs``.
    4. ``DB_DIR`` -> ``DB_DIR/logs``.
    5. ``data/<agent>/logs`` with agent default ``kronos``.
    """
    app = Path(app_dir).resolve() if app_dir is not None else _default_app_dir()
    data_dir = app / "data"
    values = dict(env or {})

    explicit_log_dir = values.get("KAOS_LOG_DIR", "").strip()
    if explicit_log_dir:
        return LogResolution(
            mode="single",
            sources=(LogSource("explicit", _abs_path(explicit_log_dir, app), "KAOS_LOG_DIR"),),
        )

    agent_name = (values.get("KAOS_AGENT_NAME") or values.get("AGENT_NAME") or "kronos").strip()
    log_mode = (values.get("KAOS_LOG_MODE") or "").strip().lower()
    if log_mode in {"aggregate", "all"} or agent_name.lower() == "all":
        sources = tuple(
            LogSource(path.parent.name, path.resolve(), "aggregate:data/*/logs")
            for path in sorted(data_dir.glob("*/logs"))
            if path.is_dir()
        )
        warnings = () if sources else (f"no log directories found under {data_dir}",)
        return LogResolution(mode="aggregate", sources=sources, warnings=warnings)

    db_path = values.get("DB_PATH", "").strip()
    if db_path:
        db = _abs_path(db_path, app)
        return LogResolution(
            mode="single",
            sources=(LogSource(db.parent.name or "db-path", db.parent / "logs", "DB_PATH"),),
        )

    db_dir = values.get("DB_DIR", "").strip()
    if db_dir:
        directory = _abs_path(db_dir, app)
        return LogResolution(
            mode="single",
            sources=(LogSource(directory.name or "db-dir", directory / "logs", "DB_DIR"),),
        )

    agent = agent_name or "kronos"
    return LogResolution(
        mode="single",
        sources=(LogSource(agent, (data_dir / agent / "logs").resolve(), "AGENT_NAME"),),
    )
