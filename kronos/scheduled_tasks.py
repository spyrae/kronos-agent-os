"""Durable user-scheduled tasks — reminders and recurring prompts (roadmap 4.2).

Backs the ``schedule_task`` tool. Rows are per-agent SQLite (survive restarts)
and polled by the cron Scheduler, which fires each due task via the
self-webhook. One-shot tasks flip to ``done``; recurring ones bump ``run_at``.
"""

import time

from kronos.db import get_db

# Human-friendly repeat presets → seconds. "none" = one-shot.
RECUR_SECONDS: dict[str, int] = {
    "none": 0,
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name    TEXT    NOT NULL,
            chat_id       INTEGER NOT NULL,
            topic_id      INTEGER,
            thread_id     TEXT    NOT NULL,
            run_at        REAL    NOT NULL,
            recur_seconds INTEGER NOT NULL DEFAULT 0,
            message       TEXT    NOT NULL,
            kind          TEXT    NOT NULL DEFAULT 'reminder',
            status        TEXT    NOT NULL DEFAULT 'pending',
            created_at    REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sched_due
            ON scheduled_tasks(status, agent_name, run_at);
        """
    )
    # Migrate DBs created before the kind column existed (roadmap 4.3).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'reminder'")


def _db():
    db = get_db("scheduled_tasks")
    db.init_schema(_init_schema)
    return db


def add_task(
    *,
    agent_name: str,
    chat_id: int,
    topic_id: int | None,
    thread_id: str,
    run_at: float,
    message: str,
    recur_seconds: int = 0,
    kind: str = "reminder",
) -> int:
    """Insert a pending task and return its id.

    kind="reminder" delivers message verbatim; kind="followup" runs message as
    an agent prompt when due and delivers the agent's result.
    """
    cursor = _db().write(
        "INSERT INTO scheduled_tasks "
        "(agent_name, chat_id, topic_id, thread_id, run_at, recur_seconds, message, kind, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (agent_name, chat_id, topic_id, thread_id, run_at, recur_seconds, message, kind, time.time()),
    )
    return int(cursor.lastrowid)


def due_tasks(agent_name: str, now: float | None = None) -> list[dict]:
    """Pending tasks for this agent whose run_at has passed."""
    now = time.time() if now is None else now
    rows = _db().read(
        "SELECT * FROM scheduled_tasks WHERE status='pending' AND agent_name=? AND run_at<=? ORDER BY run_at",
        (agent_name, now),
    )
    return [dict(row) for row in rows]


def complete_task(task_id: int, recur_seconds: int, run_at: float) -> None:
    """One-shot → done; recurring → bump run_at by one interval."""
    if recur_seconds and recur_seconds > 0:
        _db().write(
            "UPDATE scheduled_tasks SET run_at=? WHERE id=?",
            (run_at + recur_seconds, task_id),
        )
    else:
        _db().write(
            "UPDATE scheduled_tasks SET status='done' WHERE id=?",
            (task_id,),
        )


def cancel_task(task_id: int, agent_name: str) -> bool:
    """Cancel a pending task owned by this agent. Returns True if one changed."""
    cursor = _db().write(
        "UPDATE scheduled_tasks SET status='cancelled' WHERE id=? AND agent_name=? AND status='pending'",
        (task_id, agent_name),
    )
    return cursor.rowcount > 0


def list_pending(agent_name: str) -> list[dict]:
    """All pending tasks for this agent, soonest first."""
    rows = _db().read(
        "SELECT * FROM scheduled_tasks WHERE status='pending' AND agent_name=? ORDER BY run_at",
        (agent_name,),
    )
    return [dict(row) for row in rows]
