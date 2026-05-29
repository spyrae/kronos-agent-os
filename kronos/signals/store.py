"""SQLite store for normalized Signal Intelligence data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from sqlite3 import Connection, Row
from typing import Any

from kronos.db import get_db
from kronos.signals.models import SignalCluster, SignalDigest, SignalItem, StoreWriteResult, utc_now_iso
from kronos.signals.sources import SignalSource


def _schema(conn: Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signal_sources (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            handle TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL DEFAULT '',
            categories_json TEXT NOT NULL DEFAULT '[]',
            tier TEXT NOT NULL DEFAULT 'candidate',
            trust TEXT NOT NULL DEFAULT 'community_low',
            language TEXT NOT NULL DEFAULT 'en',
            enabled INTEGER NOT NULL DEFAULT 1,
            filters_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            source_item_key TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            handle TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL DEFAULT '{}',
            normalized_text TEXT NOT NULL DEFAULT '',
            categories_json TEXT NOT NULL DEFAULT '[]',
            importance_score REAL NOT NULL DEFAULT 0,
            confidence_score REAL NOT NULL DEFAULT 0,
            evidence_level TEXT NOT NULL DEFAULT 'observation',
            cluster_id INTEGER,
            duplicate_of INTEGER,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_items_source_key
            ON signal_items(source_id, source_item_key);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_items_url
            ON signal_items(url) WHERE url <> '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_items_content_hash_unique
            ON signal_items(content_hash);
        CREATE INDEX IF NOT EXISTS idx_signal_items_category
            ON signal_items(source_platform, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_items_cluster
            ON signal_items(cluster_id);

        CREATE TABLE IF NOT EXISTS signal_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            evidence_level TEXT NOT NULL DEFAULT 'observation',
            item_ids_json TEXT NOT NULL DEFAULT '[]',
            source_ids_json TEXT NOT NULL DEFAULT '[]',
            platform_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0,
            platform_count INTEGER NOT NULL DEFAULT 0,
            importance_score REAL NOT NULL DEFAULT 0,
            confidence_score REAL NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_signal_clusters_category
            ON signal_clusters(category, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS signal_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            destination TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            categories_json TEXT NOT NULL DEFAULT '[]',
            item_ids_json TEXT NOT NULL DEFAULT '[]',
            cluster_ids_json TEXT NOT NULL DEFAULT '[]',
            generated_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_signal_digests_destination
            ON signal_digests(destination, generated_at DESC);

        CREATE TABLE IF NOT EXISTS source_quality_stats (
            source_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            items_seen INTEGER NOT NULL DEFAULT 0,
            items_inserted INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            avg_importance REAL NOT NULL DEFAULT 0,
            avg_confidence REAL NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """
    )


class SignalStore:
    """Persistent store for raw/normalized signals, clusters and digests."""

    def __init__(self) -> None:
        self._db = get_db("signals")
        self._db.init_schema(_schema)

    def upsert_source(self, source: SignalSource) -> None:
        """Persist the current source registry state for auditability."""
        now = utc_now_iso()
        self._db.write(
            """
            INSERT OR REPLACE INTO signal_sources
                (id, platform, handle, url, query, categories_json, tier, trust,
                 language, enabled, filters_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.id,
                source.platform,
                source.handle,
                source.url,
                source.query,
                _json(source.categories),
                source.tier,
                source.trust,
                source.language,
                1 if source.enabled else 0,
                _json(source.filters),
                now,
            ),
        )

    def save_item(self, item: SignalItem) -> StoreWriteResult:
        """Idempotently insert a normalized signal item."""
        now = utc_now_iso()
        fetched_at = item.fetched_at or now
        content_hash = _content_hash(item)
        source_item_key = item.source_item_key or item.url or item.source_url or content_hash

        def tx(conn: Connection) -> StoreWriteResult:
            existing = _find_duplicate(
                conn,
                source_id=item.source_id,
                source_item_key=source_item_key,
                url=item.url,
                content_hash=content_hash,
            )
            if existing is not None:
                _update_quality_stats(
                    conn,
                    source_id=item.source_id,
                    platform=item.source_platform,
                    inserted=False,
                    importance=item.importance_score,
                    confidence=item.confidence_score,
                    now=now,
                )
                return StoreWriteResult(id=int(existing["id"]), inserted=False, duplicate_of=int(existing["id"]))

            cursor = conn.execute(
                """
                INSERT INTO signal_items
                    (source_id, source_platform, source_item_key, source_url, author,
                     handle, title, text, url, published_at, fetched_at,
                     raw_payload_json, normalized_text, categories_json,
                     importance_score, confidence_score, evidence_level,
                     cluster_id, duplicate_of, content_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source_id,
                    item.source_platform,
                    source_item_key,
                    item.source_url,
                    item.author,
                    item.handle,
                    item.title,
                    item.text,
                    item.url,
                    item.published_at,
                    fetched_at,
                    _json(item.raw_payload),
                    item.normalized_text,
                    _json(item.categories),
                    item.importance_score,
                    item.confidence_score,
                    item.evidence_level,
                    item.cluster_id,
                    item.duplicate_of,
                    content_hash,
                    now,
                    now,
                ),
            )
            item_id = int(cursor.lastrowid)
            _update_quality_stats(
                conn,
                source_id=item.source_id,
                platform=item.source_platform,
                inserted=True,
                importance=item.importance_score,
                confidence=item.confidence_score,
                now=now,
            )
            return StoreWriteResult(id=item_id, inserted=True)

        return self._db.write_tx(tx)

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        row = self._db.read_one("SELECT * FROM signal_items WHERE id = ?", (item_id,))
        return _decode_item(row) if row else None

    def list_items(
        self,
        *,
        category: str | None = None,
        source_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_items WHERE 1 = 1"
        params: list[Any] = []
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        if category:
            sql += " AND categories_json LIKE ?"
            params.append(f'%"{category}"%')
        sql += " ORDER BY fetched_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [_decode_item(row) for row in self._db.read(sql, tuple(params))]

    def create_cluster(self, cluster: SignalCluster) -> int:
        """Create a cluster and assign its items to it."""
        now = utc_now_iso()
        item_rows = self._items_for_ids(cluster.item_ids)
        source_ids = cluster.source_ids or tuple(sorted({row["source_id"] for row in item_rows}))
        platform_ids = cluster.platform_ids or tuple(sorted({row["source_platform"] for row in item_rows}))
        evidence_count = cluster.evidence_count or len(cluster.item_ids)
        first_seen_at = cluster.first_seen_at or _min_non_empty(row["published_at"] or row["fetched_at"] for row in item_rows)
        last_seen_at = cluster.last_seen_at or _max_non_empty(row["published_at"] or row["fetched_at"] for row in item_rows) or now

        def tx(conn: Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO signal_clusters
                    (category, title, summary, evidence_level, item_ids_json,
                     source_ids_json, platform_ids_json, evidence_count,
                     source_count, platform_count, importance_score, confidence_score,
                     first_seen_at, last_seen_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cluster.category,
                    cluster.title,
                    cluster.summary,
                    cluster.evidence_level,
                    _json(cluster.item_ids),
                    _json(source_ids),
                    _json(platform_ids),
                    evidence_count,
                    cluster.source_count or len(source_ids),
                    cluster.platform_count or len(platform_ids),
                    cluster.importance_score,
                    cluster.confidence_score,
                    first_seen_at,
                    last_seen_at,
                    now,
                    now,
                ),
            )
            cluster_id = int(cursor.lastrowid)
            if cluster.item_ids:
                placeholders = ",".join("?" for _ in cluster.item_ids)
                conn.execute(
                    f"UPDATE signal_items SET cluster_id = ?, updated_at = ? WHERE id IN ({placeholders})",
                    (cluster_id, now, *cluster.item_ids),
                )
            return cluster_id

        return int(self._db.write_tx(tx))

    def get_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        row = self._db.read_one("SELECT * FROM signal_clusters WHERE id = ?", (cluster_id,))
        return _decode_cluster(row) if row else None

    def list_clusters(self, *, category: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if category:
            rows = self._db.read(
                "SELECT * FROM signal_clusters WHERE category = ? ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = self._db.read(
                "SELECT * FROM signal_clusters ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        return [_decode_cluster(row) for row in rows]

    def get_cluster_items(self, cluster_id: int) -> list[dict[str, Any]]:
        rows = self._db.read(
            "SELECT * FROM signal_items WHERE cluster_id = ? ORDER BY fetched_at ASC, id ASC",
            (cluster_id,),
        )
        return [_decode_item(row) for row in rows]

    def save_digest(self, digest: SignalDigest) -> int:
        now = utc_now_iso()
        cursor = self._db.write(
            """
            INSERT INTO signal_digests
                (destination, title, body, categories_json, item_ids_json,
                 cluster_ids_json, generated_at, sent_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest.destination,
                digest.title,
                digest.body,
                _json(digest.categories),
                _json(digest.item_ids),
                _json(digest.cluster_ids),
                digest.generated_at or now,
                digest.sent_at,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def list_digests(self, *, destination: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if destination:
            rows = self._db.read(
                "SELECT * FROM signal_digests WHERE destination = ? ORDER BY generated_at DESC, id DESC LIMIT ?",
                (destination, limit),
            )
        else:
            rows = self._db.read(
                "SELECT * FROM signal_digests ORDER BY generated_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        return [_decode_digest(row) for row in rows]

    def get_source_quality_stats(self, source_id: str | None = None) -> list[dict[str, Any]]:
        if source_id:
            rows = self._db.read(
                "SELECT * FROM source_quality_stats WHERE source_id = ?",
                (source_id,),
            )
        else:
            rows = self._db.read(
                "SELECT * FROM source_quality_stats ORDER BY updated_at DESC, source_id ASC",
            )
        return [dict(row) for row in rows]

    def _items_for_ids(self, item_ids: tuple[int, ...]) -> list[dict[str, Any]]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        rows = self._db.read(
            f"SELECT * FROM signal_items WHERE id IN ({placeholders})",
            tuple(item_ids),
        )
        return [_decode_item(row) for row in rows]


def _find_duplicate(
    conn: Connection,
    *,
    source_id: str,
    source_item_key: str,
    url: str,
    content_hash: str,
) -> Row | None:
    row = conn.execute(
        "SELECT id FROM signal_items WHERE source_id = ? AND source_item_key = ? LIMIT 1",
        (source_id, source_item_key),
    ).fetchone()
    if row is not None:
        return row
    if url:
        row = conn.execute("SELECT id FROM signal_items WHERE url = ? LIMIT 1", (url,)).fetchone()
        if row is not None:
            return row
    return conn.execute("SELECT id FROM signal_items WHERE content_hash = ? LIMIT 1", (content_hash,)).fetchone()


def _update_quality_stats(
    conn: Connection,
    *,
    source_id: str,
    platform: str,
    inserted: bool,
    importance: float,
    confidence: float,
    now: str,
) -> None:
    row = conn.execute(
        "SELECT * FROM source_quality_stats WHERE source_id = ?",
        (source_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO source_quality_stats
                (source_id, platform, items_seen, items_inserted, duplicate_count,
                 avg_importance, avg_confidence, last_seen_at, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                platform,
                1 if inserted else 0,
                0 if inserted else 1,
                float(importance) if inserted else 0.0,
                float(confidence) if inserted else 0.0,
                now,
                now,
            ),
        )
        return

    items_seen = int(row["items_seen"]) + 1
    items_inserted = int(row["items_inserted"]) + (1 if inserted else 0)
    duplicate_count = int(row["duplicate_count"]) + (0 if inserted else 1)

    if inserted and items_inserted > 0:
        previous_inserted = max(items_inserted - 1, 0)
        avg_importance = ((float(row["avg_importance"]) * previous_inserted) + float(importance)) / items_inserted
        avg_confidence = ((float(row["avg_confidence"]) * previous_inserted) + float(confidence)) / items_inserted
    else:
        avg_importance = float(row["avg_importance"])
        avg_confidence = float(row["avg_confidence"])

    conn.execute(
        """
        UPDATE source_quality_stats
        SET platform = ?,
            items_seen = ?,
            items_inserted = ?,
            duplicate_count = ?,
            avg_importance = ?,
            avg_confidence = ?,
            last_seen_at = ?,
            updated_at = ?
        WHERE source_id = ?
        """,
        (
            platform,
            items_seen,
            items_inserted,
            duplicate_count,
            avg_importance,
            avg_confidence,
            now,
            now,
            source_id,
        ),
    )


def _content_hash(item: SignalItem) -> str:
    semantic_content = "\n".join(
        part.strip()
        for part in (item.normalized_text, item.text, item.title)
        if part and part.strip()
    )
    content = semantic_content or "\n".join(
        part.strip()
        for part in (item.url, item.source_url, item.source_id, item.source_item_key)
        if part and part.strip()
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _decode_item(row: Row) -> dict[str, Any]:
    data = dict(row)
    data["raw_payload"] = _loads_json(data.pop("raw_payload_json"), {})
    data["categories"] = _loads_json(data.pop("categories_json"), [])
    return data


def _decode_cluster(row: Row) -> dict[str, Any]:
    data = dict(row)
    data["item_ids"] = _loads_json(data.pop("item_ids_json"), [])
    data["source_ids"] = _loads_json(data.pop("source_ids_json"), [])
    data["platform_ids"] = _loads_json(data.pop("platform_ids_json"), [])
    return data


def _decode_digest(row: Row) -> dict[str, Any]:
    data = dict(row)
    data["categories"] = _loads_json(data.pop("categories_json"), [])
    data["item_ids"] = _loads_json(data.pop("item_ids_json"), [])
    data["cluster_ids"] = _loads_json(data.pop("cluster_ids_json"), [])
    return data


def _min_non_empty(values: Iterable[str]) -> str:
    non_empty = [value for value in values if value]
    return min(non_empty) if non_empty else ""


def _max_non_empty(values: Iterable[str]) -> str:
    non_empty = [value for value in values if value]
    return max(non_empty) if non_empty else ""
