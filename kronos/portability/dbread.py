"""Read-only SQLite access for bundle export/import.

Both directions need to look at a database that may not exist yet, without
creating it and without blocking a live writer. ``get_db`` would create the
file, so portability reads go through a read-only URI instead.
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("kronos.portability.dbread")


def read_rows(db_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Return rows from an existing database, or [] if it or the table is absent."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # The database exists but the table does not — an agent that never wrote
        # facts is a valid state, not an error.
        log.debug("Skipping %s: %s", db_path.name, e)
        return []
    finally:
        conn.close()
