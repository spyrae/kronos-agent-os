"""Competitive Advantage Tracker — us vs competitors feature comparison."""

import json
import logging
from datetime import datetime, timezone

from kronos.db import get_db

log = logging.getLogger("kronos.competitors.tracker")


def _tracker_schema(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS competitive_advantages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_area TEXT NOT NULL UNIQUE,
            our_status TEXT DEFAULT 'par',
            competitor_leader TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
            trend TEXT DEFAULT 'stable'
        );
    """)


class CompetitiveTracker:
    """Tracks your product's competitive position across feature areas."""

    # Default feature areas for travel planning apps
    DEFAULT_FEATURES = [
        ("ai_chat", "AI Chat Assistant"),
        ("itinerary", "Itinerary Generation"),
        ("poi", "POI Recommendations"),
        ("visa", "Visa Information"),
        ("booking_import", "Booking Import"),
        ("offline", "Offline Access"),
        ("collaboration", "Group/Collaborative Planning"),
        ("maps", "Maps Integration"),
        ("budget", "Budget Tracking"),
        ("social", "Social/Community Features"),
    ]

    def __init__(self) -> None:
        self._db = get_db("competitor_monitor")
        self._db.init_schema(_tracker_schema)
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        """Seed default feature areas if table is empty."""
        row = self._db.read_one("SELECT COUNT(*) as cnt FROM competitive_advantages")
        if row and row["cnt"] > 0:
            return

        ops = []
        for area_id, _ in self.DEFAULT_FEATURES:
            ops.append((
                "INSERT OR IGNORE INTO competitive_advantages (feature_area) VALUES (?)",
                (area_id,),
            ))
        if ops:
            self._db.write_many(ops)

    def get_all(self) -> list[dict]:
        rows = self._db.read(
            "SELECT * FROM competitive_advantages ORDER BY feature_area"
        )
        return [dict(r) for r in rows]

    def update(
        self,
        feature_area: str,
        our_status: str | None = None,
        competitor_leader: str | None = None,
        notes: str | None = None,
        trend: str | None = None,
    ) -> None:
        """Update a specific feature area."""
        sets = ["last_updated = CURRENT_TIMESTAMP"]
        params = []

        if our_status is not None:
            sets.append("our_status = ?")
            params.append(our_status)
        if competitor_leader is not None:
            sets.append("competitor_leader = ?")
            params.append(competitor_leader)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        if trend is not None:
            sets.append("trend = ?")
            params.append(trend)

        params.append(feature_area)
        self._db.write(
            f"UPDATE competitive_advantages SET {', '.join(sets)} WHERE feature_area = ?",
            tuple(params),
        )

    def bulk_update_from_llm(self, updates: list[dict]) -> None:
        """Apply LLM-generated updates to the tracker.

        Each update: {"feature_area": str, "our_status": str, "competitor_leader": str,
                       "notes": str, "trend": str}
        """
        for u in updates:
            area = u.get("feature_area", "")
            if not area:
                continue
            # Ensure the feature area exists
            self._db.write(
                "INSERT OR IGNORE INTO competitive_advantages (feature_area) VALUES (?)",
                (area,),
            )
            self.update(
                feature_area=area,
                our_status=u.get("our_status"),
                competitor_leader=u.get("competitor_leader"),
                notes=u.get("notes"),
                trend=u.get("trend"),
            )

    def format_summary(self) -> str:
        """Format tracker as text for LLM context."""
        rows = self.get_all()
        if not rows:
            return "No competitive advantages tracked yet."

        lines = ["Feature Area | Status | Leader | Trend | Notes"]
        lines.append("-" * 60)
        for r in rows:
            lines.append(
                f"{r['feature_area']} | {r['our_status']} | "
                f"{r['competitor_leader'] or '-'} | {r['trend']} | "
                f"{(r['notes'] or '-')[:50]}"
            )
        return "\n".join(lines)
