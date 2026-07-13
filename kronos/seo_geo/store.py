"""SQLite store for SEO/GEO tracking history.

Schema:
- ``positions`` — daily snapshot of [engine, keyword, position] for each site.
- ``geo_citations`` — daily snapshot of LLM answers + whether they cite us.
- ``gsc_metrics`` — daily aggregated GSC metrics (impressions, clicks, ctr).

Stored alongside the per-agent DB (``./data/<agent_name>/seo_geo.db``)
so it lives in the same backup boundary as session/memory.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from kronos.config import settings

log = logging.getLogger("kronos.seo_geo.store")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,        -- ISO 8601 UTC
    site_id TEXT NOT NULL,
    engine TEXT NOT NULL,            -- 'google_com' | 'google_ru' | 'yandex_ru'
    keyword TEXT NOT NULL,
    locale TEXT NOT NULL,
    tier TEXT NOT NULL,
    category TEXT NOT NULL,
    position INTEGER,                 -- NULL = not found in top 100
    url TEXT,                         -- the page that ranked
    error TEXT                        -- error message if check failed
);
CREATE INDEX IF NOT EXISTS idx_positions_lookup
    ON positions(site_id, engine, keyword, checked_at DESC);

CREATE TABLE IF NOT EXISTS geo_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    site_id TEXT NOT NULL,
    engine TEXT NOT NULL,             -- 'chatgpt' | 'perplexity' | 'claude' | 'gemini' | 'kimi'
    question TEXT NOT NULL,
    locale TEXT NOT NULL,
    answer TEXT NOT NULL,             -- full LLM response (truncated to 8k)
    cited INTEGER NOT NULL,           -- 1 = our brand appeared in answer
    cited_url TEXT,                   -- our URL if explicitly cited
    competitors_cited TEXT,           -- JSON array of competitor names mentioned
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_geo_citations_lookup
    ON geo_citations(site_id, engine, checked_at DESC);

CREATE TABLE IF NOT EXISTS gsc_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    site_id TEXT NOT NULL,
    window_days INTEGER NOT NULL,     -- 28, 7, etc.
    query TEXT NOT NULL,
    clicks INTEGER NOT NULL,
    impressions INTEGER NOT NULL,
    ctr REAL NOT NULL,
    position REAL NOT NULL,
    country TEXT,
    UNIQUE(site_id, checked_at, query, country)
);
CREATE INDEX IF NOT EXISTS idx_gsc_metrics_lookup
    ON gsc_metrics(site_id, checked_at DESC);
"""


class SeoGeoStore:
    """SQLite-backed store. Single-threaded usage (cron job)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.path = db_path or Path(settings.db_dir) / "seo_geo.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── positions ──────────────────────────────────────────────────────
    def record_position(
        self,
        *,
        site_id: str,
        engine: str,
        keyword: str,
        locale: str,
        tier: str,
        category: str,
        position: int | None,
        url: str | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO positions"
            " (checked_at, site_id, engine, keyword, locale, tier, category, position, url, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                site_id,
                engine,
                keyword,
                locale,
                tier,
                category,
                position,
                url,
                error,
            ),
        )
        self._conn.commit()

    def latest_positions(self, site_id: str, engine: str | None = None) -> list[dict]:
        sql = "SELECT keyword, engine, position, url, checked_at, tier FROM positions WHERE site_id = ?"
        params: list = [site_id]
        if engine:
            sql += " AND engine = ?"
            params.append(engine)
        sql += " AND checked_at = (SELECT MAX(checked_at) FROM positions p2"
        sql += " WHERE p2.site_id = positions.site_id AND p2.keyword = positions.keyword"
        sql += " AND p2.engine = positions.engine)"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def position_delta(self, site_id: str, engine: str, keyword: str, days: int = 7) -> int | None:
        """Returns position change vs `days` ago. Negative = improved."""
        cur = self._conn.execute(
            "SELECT position FROM positions WHERE site_id=? AND engine=? AND keyword=?"
            " ORDER BY checked_at DESC LIMIT 1",
            (site_id, engine, keyword),
        ).fetchone()
        prev = self._conn.execute(
            "SELECT position FROM positions WHERE site_id=? AND engine=? AND keyword=?"
            " AND checked_at <= datetime('now', ?)"
            " ORDER BY checked_at DESC LIMIT 1",
            (site_id, engine, keyword, f"-{days} days"),
        ).fetchone()
        if not cur or not prev or cur["position"] is None or prev["position"] is None:
            return None
        return cur["position"] - prev["position"]

    # ── geo_citations ─────────────────────────────────────────────────
    def record_citation(
        self,
        *,
        site_id: str,
        engine: str,
        question: str,
        locale: str,
        answer: str,
        cited: bool,
        cited_url: str | None = None,
        competitors_cited: str | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO geo_citations"
            " (checked_at, site_id, engine, question, locale, answer, cited,"
            " cited_url, competitors_cited, error) VALUES"
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                site_id,
                engine,
                question,
                locale,
                answer[:8000],
                1 if cited else 0,
                cited_url,
                competitors_cited,
                error,
            ),
        )
        self._conn.commit()

    def citation_rate(self, site_id: str, days: int = 7) -> dict[str, float]:
        """Returns {engine: pct} citation rate for the site over the last N days."""
        rows = self._conn.execute(
            "SELECT engine, AVG(cited) * 100 AS rate FROM geo_citations"
            " WHERE site_id=? AND checked_at >= datetime('now', ?)"
            " AND error IS NULL GROUP BY engine",
            (site_id, f"-{days} days"),
        ).fetchall()
        return {r["engine"]: round(r["rate"], 1) for r in rows}

    def competitor_mentions(self, site_id: str, days: int = 7) -> dict[str, int]:
        """Returns {competitor_name: count} — who LLMs mention most for this site."""
        import json as _json

        rows = self._conn.execute(
            "SELECT competitors_cited FROM geo_citations"
            " WHERE site_id=? AND checked_at >= datetime('now', ?)"
            " AND error IS NULL AND competitors_cited IS NOT NULL",
            (site_id, f"-{days} days"),
        ).fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            try:
                comps = _json.loads(r["competitors_cited"] or "[]")
            except Exception:
                continue
            for c in comps:
                key = str(c).lower()
                counter[key] = counter.get(key, 0) + 1
        return dict(sorted(counter.items(), key=lambda x: -x[1]))

    def sample_answers(self, site_id: str, days: int = 7, n: int = 3) -> list[dict]:
        """Return n representative LLM answers (mix cited + not cited)."""
        rows = self._conn.execute(
            "SELECT engine, question, locale, answer, cited, competitors_cited"
            " FROM geo_citations"
            " WHERE site_id=? AND checked_at >= datetime('now', ?)"
            " AND error IS NULL AND length(answer) > 50"
            " ORDER BY cited DESC, length(answer) DESC LIMIT ?",
            (site_id, f"-{days} days", n),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── gsc_metrics ───────────────────────────────────────────────────
    def record_gsc(
        self,
        *,
        site_id: str,
        window_days: int,
        query: str,
        clicks: int,
        impressions: int,
        ctr: float,
        position: float,
        country: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO gsc_metrics"
            " (checked_at, site_id, window_days, query, clicks, impressions,"
            "  ctr, position, country) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).strftime("%Y-%m-%d"),
                site_id,
                window_days,
                query,
                clicks,
                impressions,
                ctr,
                position,
                country,
            ),
        )
        self._conn.commit()

    def gsc_totals(self, site_id: str, days: int = 28) -> dict[str, int | float]:
        row = self._conn.execute(
            "SELECT SUM(clicks) AS clicks, SUM(impressions) AS impressions,"
            " AVG(position) AS avg_position FROM gsc_metrics"
            " WHERE site_id=? AND window_days=?",
            (site_id, days),
        ).fetchone()
        return {
            "clicks": row["clicks"] or 0,
            "impressions": row["impressions"] or 0,
            "avg_position": round(row["avg_position"] or 0.0, 1),
        }

    def gsc_top_queries(self, site_id: str, days: int = 28, limit: int = 15) -> list[dict]:
        """Return top GSC queries by impressions — concrete data the LLM can reason about."""
        rows = self._conn.execute(
            "SELECT query, clicks, impressions, ctr, position FROM gsc_metrics"
            " WHERE site_id=? AND window_days=?"
            " ORDER BY impressions DESC LIMIT ?",
            (site_id, days, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_positions_with_meta(self, site_id: str, engine: str) -> list[dict]:
        """Latest per-keyword positions with tier+category for richer reporting."""
        rows = self._conn.execute(
            "SELECT keyword, position, url, tier, category, locale"
            " FROM positions WHERE site_id=? AND engine=?"
            " AND checked_at = (SELECT MAX(checked_at) FROM positions p2"
            " WHERE p2.site_id=positions.site_id"
            " AND p2.keyword=positions.keyword AND p2.engine=positions.engine)",
            (site_id, engine),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


_singleton: SeoGeoStore | None = None


def get_store() -> SeoGeoStore:
    global _singleton
    if _singleton is None:
        _singleton = SeoGeoStore()
    return _singleton
