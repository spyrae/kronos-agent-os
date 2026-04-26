"""Application entry point — wires bridge + agent + persistence + cron."""

import asyncio
import logging
import shutil
from pathlib import Path

from kronos.config import settings
from kronos.cron.scheduler import Scheduler
from kronos.cron.setup import setup_cron_jobs
from kronos.graph import KronosAgent
from kronos.session import SessionStore
from kronos.tools.manager import managed_mcp_tools

log = logging.getLogger("kronos.app")


# Legacy data layout was flat under ./data/ and shared by all 6 agents. The
# new layout places every agent under ./data/<agent_name>/ so Qdrant/FTS/KG
# stores no longer collide across processes. Auto-migrate on startup so
# existing deployments upgrade transparently.
_LEGACY_MOVES: tuple[tuple[str, str], ...] = (
    # (legacy_relative_path, new_relative_path_inside_db_dir)
    # Per-agent flat DBs collapse into the per-agent directory.
    (f"{{agent}}.db", "session.db"),
    # Hyphenated qdrant dir (from an earlier manual isolation attempt on
    # VPS — e.g. ``data/impulse-qdrant/``) is the per-agent form we want.
    (f"{{agent}}-qdrant", "qdrant"),
    # Shared-by-accident files: the canonical "kronos" agent adopts them,
    # other agents start fresh (historical contents were corrupt anyway).
    ("memory_fts.db", "memory_fts.db"),
    ("memory_fts.db-wal", "memory_fts.db-wal"),
    ("memory_fts.db-shm", "memory_fts.db-shm"),
    ("knowledge_graph.db", "knowledge_graph.db"),
    ("knowledge_graph.db-wal", "knowledge_graph.db-wal"),
    ("knowledge_graph.db-shm", "knowledge_graph.db-shm"),
    ("mcp_registry.db", "mcp_registry.db"),
    ("qdrant", "qdrant"),
    ("logs", "logs"),
)

# Legacy names that were accidentally shared across agents. Only "kronos"
# inherits them; others start with fresh per-agent stores.
_KRONOS_ONLY_LEGACY = {
    "memory_fts.db", "memory_fts.db-wal", "memory_fts.db-shm",
    "knowledge_graph.db", "knowledge_graph.db-wal", "knowledge_graph.db-shm",
    "mcp_registry.db", "qdrant",
}


def _migrate_legacy_layout() -> None:
    """Move legacy flat ./data/* files into ./data/<agent_name>/.

    Safe to run repeatedly: skips entries where the target already exists
    or the legacy source is missing.
    """
    db_dir = Path(settings.db_dir)
    legacy_root = db_dir.parent  # ./data
    agent = settings.agent_name

    for legacy_name, new_name in _LEGACY_MOVES:
        legacy_name = legacy_name.format(agent=agent)
        src = legacy_root / legacy_name
        dst = db_dir / new_name
        if not src.exists():
            continue
        if dst.exists():
            continue
        if legacy_name in _KRONOS_ONLY_LEGACY and agent != "kronos":
            continue
        db_dir.mkdir(parents=True, exist_ok=True)
        log.warning("Migrating legacy layout: %s -> %s", src, dst)
        shutil.move(str(src), str(dst))


def _ensure_data_dirs() -> None:
    """Create required data directories at startup."""
    _migrate_legacy_layout()
    db_dir = Path(settings.db_dir)
    (db_dir / "logs").mkdir(parents=True, exist_ok=True)
    # Swarm ledger lives outside the per-agent directory.
    Path(settings.swarm_db_path).parent.mkdir(parents=True, exist_ok=True)
    # Force schema init so the first group message does not pay the cost.
    from kronos.swarm_store import get_swarm
    get_swarm()
    log.info("Data dirs ready: %s (swarm=%s)", db_dir, settings.swarm_db_path)


async def main():
    """Start Kronos II: build agent, start bridge + cron scheduler."""
    log.info("Starting Kronos II v0.1.0")
    _ensure_data_dirs()

    session_store = SessionStore(settings.db_path, agent_name=settings.agent_name)

    async with managed_mcp_tools() as tools:
        agent = KronosAgent(
            tools=tools or None,
            session_store=session_store,
        )
        log.info("Agent ready: %d tools, db=%s", len(tools), settings.db_path)

        # Start cron scheduler
        scheduler = Scheduler()
        setup_cron_jobs(scheduler)

        # Start dashboard
        from dashboard.server import run_dashboard

        from kronos.bridge import run_bridge
        from kronos.discord_bridge import run_discord

        # Run all services concurrently
        await asyncio.gather(
            run_bridge(agent),
            run_discord(agent),
            scheduler.run(),
            run_dashboard(scheduler=scheduler, agent=agent),
        )
