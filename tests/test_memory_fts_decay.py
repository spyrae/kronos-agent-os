"""Ebbinghaus decay over the FTS fact index."""

from datetime import UTC, datetime, timedelta

import pytest

from kronos import db
from kronos.memory import fts


@pytest.fixture
def fts_db(tmp_path, monkeypatch):
    """Point the FTS index at a throwaway database."""
    monkeypatch.setattr(db.settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(db, "_instances", {})
    monkeypatch.setattr(fts, "_schema_initialized", False)
    handle = fts._get_db()
    yield handle
    handle.close()


def _index_fact(handle, content: str, *, created_at: datetime, last_accessed: str | None) -> None:
    fts.index_fact(content, "user-1")
    handle.write(
        "UPDATE memory_facts SET created_at = ?, last_accessed = ? WHERE content = ?",
        (created_at.isoformat(), last_accessed, content),
    )


def _fact(handle, content: str):
    return handle.read_one("SELECT relevance, tier FROM memory_facts WHERE content = ?", (content,))


@pytest.mark.parametrize("last_accessed", [None, ""])
def test_decay_counts_from_created_at_when_fact_was_never_accessed(fts_db, last_accessed):
    """A fact that search never returned has no last_accessed — decay must not crash on it."""
    created_at = datetime.now(UTC) - timedelta(days=14)
    _index_fact(fts_db, "Prefers concise answers", created_at=created_at, last_accessed=last_accessed)

    stats = fts.decay_all_facts(half_life_days=14)

    row = _fact(fts_db, "Prefers concise answers")
    assert row["relevance"] == pytest.approx(0.5, abs=1e-3)
    assert row["tier"] == "warm"
    assert stats == {"decayed": 1, "tier_changes": 1, "archived": 0}


def test_decay_counts_from_last_accessed_when_present(fts_db):
    """An old fact that was just accessed stays fresh — created_at is only the fallback."""
    now = datetime.now(UTC)
    _index_fact(
        fts_db,
        "Works from Bali",
        created_at=now - timedelta(days=28),
        last_accessed=now.isoformat(),
    )

    stats = fts.decay_all_facts(half_life_days=14)

    row = _fact(fts_db, "Works from Bali")
    assert row["relevance"] == pytest.approx(1.0, abs=1e-3)
    assert row["tier"] == "active"
    assert stats["decayed"] == 0
