"""Metric History Store — SQLite storage for time-series metrics.

Used by anomaly detection for statistical baselines (14-day window).
Retention: 90 days, then auto-pruned.
"""

import logging
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kronos.db import get_db

log = logging.getLogger("kronos.analytics.metric_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_metric_name_date
    ON metric_history(metric_name, recorded_at);
"""

_RETENTION_DAYS = 90


@dataclass
class MetricPoint:
    metric_name: str
    value: float
    recorded_at: str


def _db():
    db = get_db("analytics_metrics")
    db.conn.executescript(_SCHEMA)
    return db


def record_metric(metric_name: str, value: float) -> None:
    """Record a single metric value."""
    db = _db()
    db.conn.execute(
        "INSERT INTO metric_history (metric_name, value) VALUES (?, ?)",
        (metric_name, value),
    )
    db.conn.commit()


def record_metrics(metrics: dict[str, float | int | None]) -> None:
    """Record multiple metrics from a pulse collection.

    Skips None values. Flattens nested dicts with dot notation.
    """
    db = _db()
    count = 0
    for key, value in metrics.items():
        if isinstance(value, dict):
            for sub_key, sub_val in value.items():
                if sub_key != "error" and isinstance(sub_val, (int, float)):
                    db.conn.execute(
                        "INSERT INTO metric_history (metric_name, value) VALUES (?, ?)",
                        (f"{key}.{sub_key}", float(sub_val)),
                    )
                    count += 1
        elif isinstance(value, (int, float)):
            db.conn.execute(
                "INSERT INTO metric_history (metric_name, value) VALUES (?, ?)",
                (key, float(value)),
            )
            count += 1
    db.conn.commit()
    if count:
        log.debug("Recorded %d metrics", count)


def get_history(metric_name: str, days: int = 14) -> list[float]:
    """Get metric values for the last N days (one per day, latest)."""
    db = _db()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = db.conn.execute(
        """SELECT value FROM metric_history
           WHERE metric_name = ? AND recorded_at >= ?
           ORDER BY recorded_at ASC""",
        (metric_name, cutoff),
    ).fetchall()
    return [r[0] for r in rows]


def get_latest(metric_name: str) -> float | None:
    """Get the most recent value for a metric."""
    db = _db()
    row = db.conn.execute(
        """SELECT value FROM metric_history
           WHERE metric_name = ?
           ORDER BY recorded_at DESC LIMIT 1""",
        (metric_name,),
    ).fetchone()
    return row[0] if row else None


def get_average(metric_name: str, days: int = 14) -> float | None:
    """Get average value over last N days."""
    values = get_history(metric_name, days)
    return statistics.mean(values) if values else None


def prune_old(days: int = _RETENTION_DAYS) -> int:
    """Delete metrics older than retention period. Returns count deleted."""
    db = _db()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cursor = db.conn.execute(
        "DELETE FROM metric_history WHERE recorded_at < ?",
        (cutoff,),
    )
    db.conn.commit()
    deleted = cursor.rowcount
    if deleted:
        log.info("Pruned %d old metric records", deleted)
    return deleted
