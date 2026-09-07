"""Versioned, additive migrations for the email-expense ledger."""

from kronos.cron.expenses.migrations.v001_expense_items import migrate


def apply_migrations(conn) -> None:
    """Upgrade existing ledgers without rewriting legacy email/pending rows."""
    migrate(conn)
