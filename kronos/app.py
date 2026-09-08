"""Application entry point — wires bridge + agent + persistence + cron."""

import asyncio
import logging
import shutil
import signal
from pathlib import Path

from kronos import __version__
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
    ("{agent}.db", "session.db"),
    # Hyphenated qdrant dir from an earlier manual isolation attempt
    # (e.g. ``data/impulse-qdrant/``) is the per-agent form we want.
    ("{agent}-qdrant", "qdrant"),
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
    "memory_fts.db",
    "memory_fts.db-wal",
    "memory_fts.db-shm",
    "knowledge_graph.db",
    "knowledge_graph.db-wal",
    "knowledge_graph.db-shm",
    "mcp_registry.db",
    "qdrant",
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


async def _run_startup_recovery(agent: KronosAgent, max_attempts: int) -> None:
    """Run one recovery pass without waiting for the transport to become ready.

    Remain a live service after the pass, so successful one-shot completion does
    not look like a stopped bridge to the application's FIRST_COMPLETED monitor.
    """
    finished = await agent.resume_abandoned_turns(notify=True, max_attempts=max_attempts)
    log.info("Startup recovery completed: %d finished turn(s)", finished)
    await asyncio.Event().wait()


async def _run_dashboard_service(scheduler: Scheduler, agent: KronosAgent) -> None:
    """Keep an explicitly disabled accessory from stopping the whole agent."""
    from dashboard.server import run_dashboard

    started = await run_dashboard(scheduler=scheduler, agent=agent)
    if started is False:
        await asyncio.Event().wait()


def _activate_policy_or_exit() -> None:
    """Load policy.yaml before any capability gate is read.

    A malformed policy stops the process instead of falling back to permissive
    defaults — the same fail-closed reasoning as the webhook secret.
    """
    from kronos.policy import PolicyError, activate_policy

    try:
        policy = activate_policy()
    except PolicyError as e:
        log.error("Refusing to start: %s", e)
        raise SystemExit(1) from e

    if policy.loaded_from_file:
        log.info("Governance policy active: %s", policy.source_path)


def _load_swarm_registry_or_exit() -> None:
    """Validate agents.yaml before the router imports it.

    The registry is read at import time and a contradiction raises there, which
    would surface as a traceback from whichever module happened to import the
    router first. Checking it here turns that into one readable line.
    """
    from kronos.swarm_config import SwarmConfigError, load_profiles

    try:
        profiles = load_profiles()
    except SwarmConfigError as e:
        log.error("Refusing to start: %s", e)
        raise SystemExit(1) from e

    if profiles:
        owned = sum(1 for profile in profiles.values() if profile.owns)
        log.info("Swarm registry: %d agent(s), %d with topic ownership", len(profiles), owned)


async def main():
    """Start Kronos Agent OS: build agent, start bridge + cron scheduler."""
    log.info("Starting Kronos Agent OS v%s", __version__)
    _activate_policy_or_exit()
    _load_swarm_registry_or_exit()
    _ensure_data_dirs()

    session_store = SessionStore(settings.db_path, agent_name=settings.agent_name)

    from kronos.policy import get_policy

    durable = get_policy().durable
    if durable.resume_mode != "resume":
        await session_store.recover_abandoned_turns()

    async with managed_mcp_tools() as tools:
        agent = KronosAgent(
            tools=tools or None,
            session_store=session_store,
        )
        log.info("Agent ready: %d tools, db=%s", len(tools), settings.db_path)

        # Startup owns reconciliation; the first incoming message must not
        # run report-mode recovery against a concurrently resuming turn.
        agent._durable_recovery_checked = True

        # Start cron scheduler
        scheduler = Scheduler()
        setup_cron_jobs(scheduler)

        from kronos.bridge import run_bridge
        from kronos.cron.delivery import run_delivery_worker
        from kronos.discord_bridge import run_discord

        # Run all services concurrently with graceful shutdown. A SIGTERM
        # (systemd stop) or SIGINT (Ctrl-C) — or any service returning/crashing
        # — stops the scheduler and cancels the rest so in-flight turns and MCP
        # sessions unwind instead of being killed mid-flight.
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # e.g. Windows / non-main thread — rely on cancellation

        services = [
            asyncio.create_task(run_bridge(agent), name="bridge"),
            asyncio.create_task(run_discord(agent), name="discord"),
            asyncio.create_task(scheduler.run(), name="scheduler"),
            asyncio.create_task(_run_dashboard_service(scheduler, agent), name="dashboard"),
            asyncio.create_task(run_delivery_worker(session_store), name="delivery"),
        ]
        if durable.resume_mode == "resume":
            services.append(
                asyncio.create_task(_run_startup_recovery(agent, durable.max_resume_attempts), name="recovery")
            )
        stop_task = asyncio.create_task(stop_event.wait(), name="stop")

        try:
            done, _pending = await asyncio.wait([*services, stop_task], return_when=asyncio.FIRST_COMPLETED)
        finally:
            log.info("Shutting down services…")
            scheduler.stop()
            for task in (*services, stop_task):
                task.cancel()
            await asyncio.gather(*services, stop_task, return_exceptions=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.remove_signal_handler(sig)
                except NotImplementedError:
                    pass

        # Re-raise a genuine service crash (not a clean signal) so systemd's
        # Restart=on-failure can act on it.
        for task in services:
            if task in done and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    raise exc
        if not stop_event.is_set():
            for task in services:
                if task in done:
                    state = "cancelled" if task.cancelled() else "stopped"
                    raise RuntimeError(f"Service {task.get_name()} {state} unexpectedly")
