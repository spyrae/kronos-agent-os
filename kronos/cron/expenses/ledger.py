"""Persistent ledger for the email-expenses pipeline.

Per-agent SQLite ledger (``data/<agent>/expenses_ledger.db``). An additive
migration introduces ``email_expense_items``: a frozen extraction and individual
outcomes, so retrying a partial email does not repeat successful writes.

Legacy tables remain readable:

* ``processed_emails`` — idempotency + per-message audit trail. One row per
  Gmail message we have acted on, keyed by ``message_id``. Guarantees a message
  is never turned into a second Notion expense on re-runs, records whether the
  source email was archived, and carries ``amount_idr`` + ``expense_date`` so a
  later email (e.g. the bank's copy of a Grab charge) can be recognised as a
  cross-source duplicate.

* ``pending_expenses`` — expenses the pipeline could not write: an unsupported
  currency, or a charge the audit pass rejected. Held here (NOT written to
  Notion, email NOT archived) until the user resolves it from chat, then
  promoted to a real expense. An unclear category no longer lands here — it is
  recorded under the fallback category instead.

State machines::

    processed_emails.status : recorded | archived | skipped | duplicate | error | pending
    pending_expenses.status : pending | resolving | uncertain | resolved | discarded

``recorded`` means the Notion page exists; ``archived`` means the email was also
removed from the inbox. ``skipped`` covers non-expenses (top-ups, transfers,
marketing). Definite errors retry; pending and uncertain writes remain visible
and non-terminal. Unknown external outcomes require manual reconciliation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime

from kronos.cron.expenses.migrations import apply_migrations
from kronos.db import SafeDB, get_db

log = logging.getLogger("kronos.cron.expenses.ledger")

LEDGER_DB_NAME = "expenses_ledger"

# Terminal processed states — a message in any of these is never reprocessed.
# ``error`` is intentionally excluded so failed messages retry on the next run.
DONE_STATUSES = ("recorded", "archived", "skipped", "duplicate")
PROCESSED_STATUSES = frozenset(DONE_STATUSES) | {"error", "pending"}
PENDING_STATUSES = frozenset({"pending", "resolving", "uncertain", "resolved", "discarded"})

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
    apply_migrations(conn)


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

    def list_retryable(self, limit: int = 25):
        """Return failed emails even after they leave the Gmail search window."""
        return self._db.read(
            "SELECT message_id, source FROM ("
            "SELECT message_id, source, updated_at FROM processed_emails AS p WHERE status = 'error' "
            "AND NOT EXISTS (SELECT 1 FROM email_expense_items WHERE message_id = p.message_id) "
            "UNION ALL SELECT message_id, source, updated_at FROM email_expense_items AS i "
            "WHERE (status IN ('ready', 'error') OR NOT EXISTS ("
            "SELECT 1 FROM email_expense_items AS sibling WHERE sibling.message_id = i.message_id "
            "AND sibling.status NOT IN ('recorded', 'duplicate', 'discarded'))) "
            "AND NOT EXISTS (SELECT 1 FROM processed_emails AS p WHERE p.message_id = i.message_id "
            "AND p.status IN ('recorded', 'archived', 'duplicate', 'skipped'))"
            ") GROUP BY message_id, source ORDER BY MIN(updated_at), message_id LIMIT ?",
            (limit,),
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
                message_id,
                source,
                status,
                amount,
                currency,
                amount_idr,
                expense_date,
                description,
                category,
                notion_page_id,
                int(archived),
                error,
                now,
                now,
            ),
        )

    def mark_archived(self, message_id: str) -> None:
        """Flag the source email as removed from the inbox (status → archived)."""
        self._db.write(
            "UPDATE processed_emails SET archived = 1, status = 'archived', updated_at = ? WHERE message_id = ?",
            (_now(), message_id),
        )

    def find_recorded_duplicate(
        self, amount_idr: float | None, expense_date: str | None, *, exclude_message_id: str = ""
    ):
        """Return an already-recorded expense with the same IDR amount and date.

        This is the cross-source dedup primitive: when Grab and the bank both
        email about one card charge, the second email matches the first's row
        here (same ``amount_idr`` + ``expense_date``) and is skipped as a
        duplicate. Returns None when ``amount_idr`` is unknown (RUB/USD spends
        are not IDR-dedupable) or nothing matches.
        """
        if amount_idr is None or expense_date is None:
            return None
        item = self._db.read_one(
            "SELECT * FROM email_expense_items WHERE expense_date = ? AND amount_idr = ? "
            "AND status = 'recorded' AND message_id != ? LIMIT 1",
            (expense_date, amount_idr, exclude_message_id),
        )
        return item or self._db.read_one(
            "SELECT * FROM processed_emails WHERE expense_date = ? AND amount_idr = ? "
            "AND status IN ('recorded', 'archived') AND message_id != ? LIMIT 1",
            (expense_date, amount_idr, exclude_message_id),
        )

    # ── per-item progress ────────────────────────────────────────────────

    def list_items(self, message_id: str):
        """Return the frozen extraction in its original order."""
        return self._db.read(
            "SELECT * FROM email_expense_items WHERE message_id = ? ORDER BY item_index",
            (message_id,),
        )

    def prepare_items(self, message_id: str, source: str, expenses: list) -> list:
        """Save a whole extraction once, before the first external write."""

        def save(conn):
            if conn.execute("SELECT 1 FROM email_expense_items WHERE message_id = ?", (message_id,)).fetchone():
                return
            for index, expense in enumerate(expenses):
                payload = asdict(expense)
                payload.pop("raw", None)
                conn.execute(
                    "INSERT INTO email_expense_items "
                    "(message_id, item_index, source, expense_json, amount_idr, expense_date, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        index,
                        source,
                        json.dumps(payload),
                        expense.amount if expense.currency == "IDR" else None,
                        expense.expense_date,
                        _now(),
                    ),
                )

        self._db.write_tx(save)
        return self.list_items(message_id)

    def needs_processing(self, message_id: str) -> bool:
        """Retry failed items even when a different item is waiting for the user."""
        if self.is_processed(message_id):
            return False
        items = self.list_items(message_id)
        if items:
            return any(row["status"] in {"ready", "error"} for row in items) or all(
                row["status"] in {"recorded", "duplicate", "discarded"} for row in items
            )
        return not self.has_pending(message_id)

    def claim_item(self, message_id: str, item_index: int) -> bool:
        """Atomically reserve one item; an interrupted write is not auto-replayed."""
        return (
            self._db.write(
                "UPDATE email_expense_items SET status = 'writing', updated_at = ? "
                "WHERE message_id = ? AND item_index = ? AND status IN ('ready', 'error')",
                (_now(), message_id, item_index),
            ).rowcount
            == 1
        )

    def finish_item(self, message_id: str, item_index: int, status: str) -> None:
        """Commit a known outcome without overwriting a concurrent manual resolution."""
        self._db.write(
            "UPDATE email_expense_items SET status = ?, updated_at = ? "
            "WHERE message_id = ? AND item_index = ? AND status = 'writing' AND pending_id IS NULL",
            (status, _now(), message_id, item_index),
        )

    def uncertain_items(self):
        """List writes needing reconciliation, including interrupted in-flight calls."""
        return self._db.read(
            "SELECT message_id, item_index, source FROM email_expense_items "
            "WHERE status IN ('writing', 'uncertain') "
            "UNION ALL SELECT message_id, -1 AS item_index, source FROM pending_expenses AS p "
            "WHERE p.status IN ('resolving', 'uncertain') "
            "AND NOT EXISTS (SELECT 1 FROM email_expense_items WHERE pending_id = p.id) "
            "ORDER BY message_id, item_index",
        )

    def finalize_message(self, message_id: str, source: str) -> str:
        """Derive email completion from every item and every manual pending row."""

        def finalize(conn):
            items = conn.execute("SELECT * FROM email_expense_items WHERE message_id = ?", (message_id,)).fetchall()
            pending = conn.execute("SELECT * FROM pending_expenses WHERE message_id = ?", (message_id,)).fetchall()
            has_pending = any(row["status"] not in {"resolved", "discarded"} for row in pending)
            if items:
                states = {row["status"] for row in items}
                if states & {"ready", "error", "writing", "uncertain"}:
                    status = "error"
                elif "pending" in states or has_pending:
                    status = "pending"
                elif "recorded" in states:
                    status = "recorded"
                elif "duplicate" in states:
                    status = "duplicate"
                else:
                    status = "skipped"
                representative = next((row for row in items if row["status"] == "recorded"), None)
            else:
                # Older queues have no extraction snapshot. Their siblings must
                # still all be handled before the email becomes terminal.
                if not pending:
                    return "error"
                status = (
                    "pending"
                    if has_pending
                    else ("recorded" if any(row["status"] == "resolved" for row in pending) else "skipped")
                )
                representative = next((row for row in pending if row["status"] == "resolved"), None)
            conn.execute(
                "INSERT INTO processed_emails "
                "(message_id, source, status, amount_idr, expense_date, processed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
                "status=excluded.status, amount_idr=excluded.amount_idr, expense_date=excluded.expense_date, "
                "updated_at=excluded.updated_at",
                (
                    message_id,
                    source,
                    status,
                    representative["amount_idr"] if representative else None,
                    representative["expense_date"] if representative else None,
                    _now(),
                    _now(),
                ),
            )
            return status

        return self._db.write_tx(finalize)

    def pending_reference(self, pending_id: int) -> str:
        """Reuse the same reference for automatic and manually resolved items."""
        row = self._db.read_one("SELECT * FROM email_expense_items WHERE pending_id = ?", (pending_id,))
        if row is None:
            pending = self.get_pending(pending_id)
            return pending["message_id"] if pending else ""
        count = len(self.list_items(row["message_id"]))
        return row["message_id"] if count == 1 else f"{row['message_id']}:{row['item_index'] + 1}"

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
        item_index: int | None = None,
    ) -> int:
        """Queue an expense whose category is unclear. Returns the pending id."""

        def save(conn):
            cursor = conn.execute(
                """
            INSERT INTO pending_expenses
                (message_id, source, description, amount, currency, amount_idr,
                 expense_date, guessed_category, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
                (
                    message_id,
                    source,
                    description,
                    amount,
                    currency,
                    amount_idr,
                    expense_date,
                    guessed_category,
                    reason,
                    _now(),
                ),
            )
            pending_id = int(cursor.lastrowid)
            if item_index is not None:
                changed = conn.execute(
                    "UPDATE email_expense_items SET pending_id = ?, status = 'pending', updated_at = ? "
                    "WHERE message_id = ? AND item_index = ? AND status = 'writing'",
                    (pending_id, _now(), message_id, item_index),
                ).rowcount
                if changed != 1:
                    raise ValueError("pending item is not owned by this processor")
            return pending_id

        return self._db.write_tx(save)

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

    def claim_pending(self, pending_id: int) -> bool:
        """Reserve a manual resolution so two chat calls cannot write it twice."""

        def claim(conn):
            changed = conn.execute(
                "UPDATE pending_expenses SET status = 'resolving' WHERE id = ? AND status = 'pending'",
                (pending_id,),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE email_expense_items SET status = 'writing', updated_at = ? WHERE pending_id = ?",
                    (_now(), pending_id),
                )
            return bool(changed)

        return self._db.write_tx(claim)

    def release_pending_claim(self, pending_id: int, *, uncertain: bool = False) -> None:
        """Retry only definite failures; retain ambiguous writes for reconciliation."""
        status = "uncertain" if uncertain else "pending"
        self._db.write_many(
            [
                ("UPDATE pending_expenses SET status = ? WHERE id = ? AND status = 'resolving'", (status, pending_id)),
                (
                    "UPDATE email_expense_items SET status = ?, updated_at = ? WHERE pending_id = ? AND status = 'writing'",
                    (status, _now(), pending_id),
                ),
            ]
        )

    def resolve_pending(self, pending_id: int, category: str) -> None:
        """Mark a pending expense resolved with the user-chosen category."""
        self._db.write_many(
            [
                (
                    "UPDATE pending_expenses SET status = 'resolved', resolved_category = ?, resolved_at = ? WHERE id = ?",
                    (category, _now(), pending_id),
                ),
                (
                    "UPDATE email_expense_items SET status = 'recorded', updated_at = ? WHERE pending_id = ?",
                    (_now(), pending_id),
                ),
            ]
        )

    def discard_pending(self, pending_id: int) -> bool:
        """Drop a pending expense the user decided is not a real expense."""

        def discard(conn):
            changed = conn.execute(
                "UPDATE pending_expenses SET status = 'discarded', resolved_at = ? WHERE id = ? AND status = 'pending'",
                (_now(), pending_id),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE email_expense_items SET status = 'discarded', updated_at = ? WHERE pending_id = ?",
                    (_now(), pending_id),
                )
            return bool(changed)

        return self._db.write_tx(discard)

    def has_pending(self, message_id: str) -> bool:
        """True if this email already has an open pending row (avoid re-queueing)."""
        row = self._db.read_one(
            "SELECT 1 FROM pending_expenses WHERE message_id = ? AND status IN ('pending', 'resolving', 'uncertain') LIMIT 1",
            (message_id,),
        )
        return row is not None

    # ── meta / watermark ────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self._db.read_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.write(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_ledger() -> ExpenseLedger:
    """Return the ledger bound to the per-agent expenses database."""
    return ExpenseLedger(get_db(LEDGER_DB_NAME))
