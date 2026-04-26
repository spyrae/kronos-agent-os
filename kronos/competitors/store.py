"""SQLite storage for competitor snapshots and changes."""

import json
import logging

from kronos.db import get_db

log = logging.getLogger("kronos.competitors.store")


def _schema(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS competitors (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tier INTEGER DEFAULT 2,
            config JSON
        );

        CREATE TABLE IF NOT EXISTS competitor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            data JSON NOT NULL,
            captured_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS competitor_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            change_type TEXT NOT NULL,
            severity TEXT DEFAULT 'info',
            summary TEXT NOT NULL,
            details JSON,
            detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            included_in_digest INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_comp_channel
            ON competitor_snapshots(competitor_id, channel, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_changes_detected
            ON competitor_changes(detected_at DESC);
        CREATE INDEX IF NOT EXISTS idx_changes_undigested
            ON competitor_changes(included_in_digest) WHERE included_in_digest = 0;
    """)


class CompetitorStore:
    """Persistent storage for competitor monitoring data."""

    def __init__(self) -> None:
        self._db = get_db("competitor_monitor")
        self._db.init_schema(_schema)

    def upsert_competitor(self, comp_id: str, name: str, tier: int, config: dict) -> None:
        self._db.write(
            "INSERT OR REPLACE INTO competitors (id, name, tier, config) VALUES (?, ?, ?, ?)",
            (comp_id, name, tier, json.dumps(config)),
        )

    def save_snapshot(self, competitor_id: str, channel: str, data: dict) -> int:
        cursor = self._db.write(
            "INSERT INTO competitor_snapshots (competitor_id, channel, data) VALUES (?, ?, ?)",
            (competitor_id, channel, json.dumps(data)),
        )
        return cursor.lastrowid

    def get_latest_snapshot(self, competitor_id: str, channel: str) -> dict | None:
        row = self._db.read_one(
            "SELECT data FROM competitor_snapshots "
            "WHERE competitor_id = ? AND channel = ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (competitor_id, channel),
        )
        if row:
            return json.loads(row["data"])
        return None

    def save_change(
        self,
        competitor_id: str,
        channel: str,
        change_type: str,
        severity: str,
        summary: str,
        details: dict | None = None,
    ) -> int:
        cursor = self._db.write(
            "INSERT INTO competitor_changes "
            "(competitor_id, channel, change_type, severity, summary, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (competitor_id, channel, change_type, severity, summary,
             json.dumps(details) if details else None),
        )
        return cursor.lastrowid

    def mark_digested(self, change_ids: list[int]) -> None:
        if not change_ids:
            return
        placeholders = ",".join("?" for _ in change_ids)
        self._db.write(
            f"UPDATE competitor_changes SET included_in_digest = 1 "
            f"WHERE id IN ({placeholders})",
            tuple(change_ids),
        )

    def get_undigested_changes(self) -> list[dict]:
        rows = self._db.read(
            "SELECT * FROM competitor_changes WHERE included_in_digest = 0 "
            "ORDER BY detected_at DESC",
        )
        return [dict(r) for r in rows]

    def get_snapshot_count(self, competitor_id: str) -> int:
        row = self._db.read_one(
            "SELECT COUNT(*) as cnt FROM competitor_snapshots WHERE competitor_id = ?",
            (competitor_id,),
        )
        return row["cnt"] if row else 0
