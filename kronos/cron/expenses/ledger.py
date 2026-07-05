"""Persistent ledger for the email-expenses pipeline.

Two tables in a per-agent SQLite file (``data/<agent>/expenses_ledger.db``),
plus a small key/value ``meta`` table for the run watermark:

* ``processed_emails`` — idempotency + per-message audit trail. One row per
  Gmail message we have acted on, keyed by ``message_id``. Guarantees a message
  is never turned into a second Notion expense on re-runs, records whether the
  source email was archived, and carries ``amount_idr`` + ``expense_date`` so a
  later email (e.g. the bank's copy of a Grab charge) can be recognised as a
  cross-source duplicate.

* ``pending_expenses`` — expenses whose category the extractor/auditor could not
  determine confidently. Held here (NOT written to Notion, email NOT archived)
  until the user resolves the category from chat, then promoted to a real expense.

State machines::

    processed_emails.status : recorded | archived | skipped | duplicate | error
    pending_expenses.status : pending  | resolved  | discarded

``recorded`` means the Notion page exists; ``archived`` means the email was also
removed from the inbox. ``skipped`` covers non-expenses (top-ups, transfers,
marketing). ``error`` rows are retried on the next run — everything else is final.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kronos.db import SafeDB, get_db

log = logging.getLogger("kronos.cron.expenses.ledger")

LEDGER_DB_NAME = "expenses_ledger"

# Terminal processed states — a message in any of these is never reprocessed.
# ``error`` is intentionally excluded so failed messages retry on the next run.
DONE_STATUSES = ("recorded", "archived", "skipped", "duplicate")
PROCESSED_STATUSES = frozenset(DONE_STATUSES) | {"error"}
PENDING_STATUSES = frozenset({"pending", "resolved", "discarded"})

WATERMARK_KEY = "last_processed_email_ts"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id     TEXT PRIMARY KEY,
    source         TEXT,
    status         TEXT NOT NULL,
    amount         REAL,
    currency       TEXT,
    amount_idr     REAL,
    expense_date   TEXT,
    description    TEXT,
    category       TEXT,
    notion_page_id TEXT,
    archived       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    processed_at   TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_dedup
    ON processed_emails(expense_date, amount_idr);

CREATE TABLE IF NOT EXISTS pending_expenses (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        TEXT,
    source            TEXT,
    description       TEXT,
    amount            REAL,
    currency          TEXT,
    amount_idr        REAL,
    expense_date      TEXT,
    guessed_category  TEXT,
    reason            TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    resolved_category TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_expenses(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _init_schema(conn) -> None:
    conn.executescript(_SCHEMA)


class ExpenseLedger:
    """Thread-safe accessor over the email-expenses ledger tables."""

    def __init__(self, db: SafeDB):
        self._db = db
        self._db.init_schema(_init_schema)

    # ── processed_emails ────────────────────────────────────────────────

    def is_processed(self, message_id: str) -> bool:
        """True if this email was already handled to a terminal state.

        ``error`` rows return False so a transient failure is retried next run.
        """
        row = self._db.read_one(
            "SELECT status FROM processed_emails WHERE message_id = ?",
            (message_id,),
        )
        return row is not None and row["status"] in DONE_STATUSES

    def get(self, message_id: str):
        return self._db.read_one(
            "SELECT * FROM processed_emails WHERE message_id = ?",
            (message_id,),
        )

    def record(
        self,
        *,
        message_id: str,
        source: str,
        status: str,
        amount: float | None = None,
        currency: str | None = None,
        amount_idr: float | None = None,
        expense_date: str | None = None,
        description: str | None = None,
        category: str | None = None,
        notion_page_id: str | None = None,
        archived: bool = False,
        error: str | None = None,
    ) -> None:
        """Upsert a processed-email row. Re-runs update the existing row."""
        if status not in PROCESSED_STATUSES:
            raise ValueError(f"invalid processed status: {status!r}")
        now = _now()
        self._db.write(
            """
            INSERT INTO processed_emails
                (message_id, source, status, amount, currency, amount_idr,
                 expense_date, description, category, notion_page_id, archived,
                 error, processed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                source=excluded.source,
                status=excluded.status,
                amount=excluded.amount,
                currency=excluded.currency,
                amount_idr=excluded.amount_idr,
                expense_date=excluded.expense_date,
                description=excluded.description,
                category=excluded.category,
                notion_page_id=excluded.notion_page_id,
                archived=excluded.archived,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                message_id, source, status, amount, currency, amount_idr,
                expense_date, description, category, notion_page_id,
                int(archived), error, now, now,
            ),
        )

    def mark_archived(self, message_id: str) -> None:
        """Flag the source email as removed from the inbox (status → archived)."""
        self._db.write(
            "UPDATE processed_emails SET archived = 1, status = 'archived', "
            "updated_at = ? WHERE message_id = ?",
            (_now(), message_id),
        )

    def find_recorded_duplicate(self, amount_idr: float | None, expense_date: str | None):
        """Return an already-recorded expense with the same IDR amount and date.

        This is the cross-source dedup primitive: when Grab and the bank both
        email about one card charge, the second email matches the first's row
        here (same ``amount_idr`` + ``expense_date``) and is skipped as a
        duplicate. Returns None when ``amount_idr`` is unknown (RUB/USD spends
        are not IDR-dedupable) or nothing matches.
        """
        if amount_idr is None or expense_date is None:
            return None
        return self._db.read_one(
            "SELECT * FROM processed_emails WHERE expense_date = ? AND amount_idr = ? "
            "AND status IN ('recorded', 'archived') LIMIT 1",
            (expense_date, amount_idr),
        )

    # ── pending_expenses ────────────────────────────────────────────────

    def add_pending(
        self,
        *,
        message_id: str,
        source: str,
        description: str,
        amount: float | None,
        currency: str | None,
        amount_idr: float | None,
        expense_date: str | None,
        guessed_category: str | None,
        reason: str,
    ) -> int:
        """Queue an expense whose category is unclear. Returns the pending id."""
        cursor = self._db.write(
            """
            INSERT INTO pending_expenses
                (message_id, source, description, amount, currency, amount_idr,
                 expense_date, guessed_category, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                message_id, source, description, amount, currency, amount_idr,
                expense_date, guessed_category, reason, _now(),
            ),
        )
        return int(cursor.lastrowid)

    def list_pending(self, status: str = "pending") -> list:
        return self._db.read(
            "SELECT * FROM pending_expenses WHERE status = ? ORDER BY id",
            (status,),
        )

    def get_pending(self, pending_id: int):
        return self._db.read_one(
            "SELECT * FROM pending_expenses WHERE id = ?",
            (pending_id,),
        )

    def resolve_pending(self, pending_id: int, category: str) -> None:
        """Mark a pending expense resolved with the user-chosen category."""
        self._db.write(
            "UPDATE pending_expenses SET status = 'resolved', resolved_category = ?, "
            "resolved_at = ? WHERE id = ?",
            (category, _now(), pending_id),
        )

    def discard_pending(self, pending_id: int) -> None:
        """Drop a pending expense the user decided is not a real expense."""
        self._db.write(
            "UPDATE pending_expenses SET status = 'discarded', resolved_at = ? WHERE id = ?",
            (_now(), pending_id),
        )

    def has_pending(self, message_id: str) -> bool:
        """True if this email already has an open pending row (avoid re-queueing)."""
        row = self._db.read_one(
            "SELECT 1 FROM pending_expenses WHERE message_id = ? AND status = 'pending' LIMIT 1",
            (message_id,),
        )
        return row is not None

    # ── meta / watermark ────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self._db.read_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.write(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_ledger() -> ExpenseLedger:
    """Return the ledger bound to the per-agent expenses database."""
    return ExpenseLedger(get_db(LEDGER_DB_NAME))
