"""Cron scheduler — runs tasks on schedule using asyncio.

Simple cron-like scheduler without external dependencies (no APScheduler).
Runs inside the main event loop alongside bridge and dashboard.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger("kronos.cron.scheduler")

# Persistence file for last_run timestamps (survives restarts)
_STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "cron_state.json"

# UTC+8 timezone for scheduling
UTC8 = timezone(timedelta(hours=8))


@dataclass
class CronJob:
    """A scheduled task."""
    name: str
    func: Callable[[], Awaitable[None]]
    interval_seconds: int | None = None  # for periodic tasks
    cron_hour: int | None = None  # for daily tasks (UTC)
    cron_weekday: int | None = None  # 0=Monday, 6=Sunday (for weekly)
    enabled: bool = True
    last_run: float = 0.0
    _running: bool = False


class Scheduler:
    """Lightweight async cron scheduler."""

    def __init__(self):
        self.jobs: dict[str, CronJob] = {}
        self._stop = asyncio.Event()

    def add(self, job: CronJob) -> None:
        self.jobs[job.name] = job
        log.info("Registered cron job: %s", job.name)

    def _load_state(self) -> None:
        """Restore last_run timestamps from disk."""
        if not _STATE_FILE.exists():
            return
        try:
            state = json.loads(_STATE_FILE.read_text())
            for name, ts in state.items():
                if name in self.jobs:
                    self.jobs[name].last_run = float(ts)
            log.info("Restored cron state for %d jobs", len(state))
        except Exception as e:
            log.warning("Failed to load cron state: %s", e)

    def _save_state(self) -> None:
        """Persist last_run timestamps to disk."""
        state = {name: job.last_run for name, job in self.jobs.items() if job.last_run > 0}
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps(state))
        except Exception as e:
            log.warning("Failed to save cron state: %s", e)

    def add_periodic(
        self, name: str, func: Callable[[], Awaitable[None]], interval_seconds: int
    ) -> None:
        self.add(CronJob(name=name, func=func, interval_seconds=interval_seconds))

    def add_daily(
        self, name: str, func: Callable[[], Awaitable[None]], hour_utc: int
    ) -> None:
        self.add(CronJob(name=name, func=func, cron_hour=hour_utc))

    def add_weekly(
        self, name: str, func: Callable[[], Awaitable[None]], weekday: int, hour_utc: int
    ) -> None:
        self.add(CronJob(name=name, func=func, cron_hour=hour_utc, cron_weekday=weekday))

    def _should_run(self, job: CronJob) -> bool:
        if not job.enabled or job._running:
            return False

        now = time.time()

        # Periodic job
        if job.interval_seconds:
            return (now - job.last_run) >= job.interval_seconds

        # Daily/weekly job
        if job.cron_hour is not None:
            now_utc = datetime.now(timezone.utc)

            # Weekly: check weekday first
            if job.cron_weekday is not None and now_utc.weekday() != job.cron_weekday:
                return False

            # Check hour
            if now_utc.hour != job.cron_hour:
                return False

            # Only run once per scheduled hour (check last_run)
            if job.last_run > 0:
                last_dt = datetime.fromtimestamp(job.last_run, tz=timezone.utc)
                # Same calendar hour = already ran
                if last_dt.date() == now_utc.date() and last_dt.hour == now_utc.hour:
                    return False
                # For weekly jobs, same calendar day = already ran
                if job.cron_weekday is not None and last_dt.date() == now_utc.date():
                    return False

            return True

        return False

    async def _run_job(self, job: CronJob) -> None:
        job._running = True
        job.last_run = time.time()
        log.info("[cron] Starting: %s", job.name)
        start = time.monotonic()

        try:
            await job.func()
            duration = time.monotonic() - start
            log.info("[cron] Completed: %s (%.1fs)", job.name, duration)
        except Exception as e:
            duration = time.monotonic() - start
            log.error("[cron] Failed: %s (%.1fs): %s", job.name, duration, e)
        finally:
            job._running = False
            self._save_state()

    async def run(self) -> None:
        """Run scheduler loop. Call this as asyncio task."""
        log.info("Scheduler started with %d jobs: %s", len(self.jobs), list(self.jobs.keys()))
        self._load_state()

        # Wait for bridge/webhook to be ready before first run
        await asyncio.sleep(30)

        while not self._stop.is_set():
            for job in self.jobs.values():
                if self._should_run(job):
                    asyncio.create_task(self._run_job(job))

            # Check every 30 seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass

        log.info("Scheduler stopped")

    def stop(self) -> None:
        self._stop.set()
