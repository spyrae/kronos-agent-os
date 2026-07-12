"""Thread-safe SQLite connection manager.

Solves the fundamental "database is locked" problem:
- Single connection per database (singleton)
- ALL operations serialized through threading.Lock
  (sqlite3 connection objects are NOT thread-safe)
- WAL mode for file-level concurrency
- Auto-recovery: rollback + reconnect on persistent errors

Usage:
    from kronos.db import get_db

    db = get_db("memory_fts")
    rows = db.read("SELECT * FROM facts WHERE user_id = ?", (uid,))
    db.write("INSERT INTO facts VALUES (?, ?)", (uid, content))
    db.write_many([
        ("INSERT INTO facts ...", (uid, c1)),
        ("INSERT INTO fts ...", (rid, c1, uid)),
    ])
"""

import logging
import sqlite3
import threading
from pathlib import Path

from kronos.config import settings

log = logging.getLogger("kronos.db")

_instances: dict[str, "SafeDB"] = {}
_instances_lock = threading.Lock()


class SafeDB:
    """Thread-safe SQLite wrapper.

    ALL operations (reads AND writes) go through a single lock because
    sqlite3 connection objects are not thread-safe — concurrent operations
    on the same connection from different threads can corrupt internal state,
    even in WAL mode. WAL handles file-level concurrency; this lock handles
    connection-level safety.
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self) -> None:
        """Create or recreate the connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=30,
            isolation_level=None,  # autocommit — we manage transactions explicitly
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA wal_autocheckpoint=100")
        self._conn.row_factory = sqlite3.Row
        log.info("SafeDB connected: %s", self._db_path.name)

    @property
    def conn(self) -> sqlite3.Connection:
        """Raw connection — only use inside init_schema(). Thread-unsafe."""
        if self._conn is None:
            self._connect()
        return self._conn

    def init_schema(self, fn) -> None:
        """Run schema init function under lock (thread-safe, runs once).

        Note: fn should use executescript() for DDL (it manages its own transactions).
        """
        with self._lock:
            fn(self._conn)
            # Ensure no dangling transaction after schema init
            try:
                self._conn.execute("COMMIT")
            except Exception:
                pass

    def read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a read query."""
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                self._rollback_safe()
                return self._conn.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """Execute a read query and return first row."""
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchone()
            except sqlite3.OperationalError:
                self._rollback_safe()
                return self._conn.execute(sql, params).fetchone()

    def write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a single write in an IMMEDIATE transaction."""
        return self.write_tx(lambda conn: conn.execute(sql, params))

    def write_many(self, operations: list[tuple[str, tuple]]) -> None:
        """Execute multiple writes in a single IMMEDIATE transaction."""

        def _do(conn: sqlite3.Connection) -> None:
            for sql, params in operations:
                conn.execute(sql, params)

        self.write_tx(_do)

    def write_tx(self, fn) -> any:
        """Execute a function within a locked IMMEDIATE transaction.

        Uses BEGIN IMMEDIATE to acquire the write lock upfront, preventing
        "database is locked" during FTS5 operations.

        fn receives the connection and should do reads+writes. Commit happens
        after fn returns. On ANY error the transaction is rolled back before it
        propagates — a leaked BEGIN would wedge the connection's write lock for
        every later write. A transient OperationalError ("database is locked")
        is retried once; other errors (IntegrityError, a raise inside fn) roll
        back and propagate without a retry that would just fail again.

        Usage:
            def update(conn):
                row = conn.execute("SELECT ...").fetchone()
                conn.execute("INSERT ...", (...))
                return row["id"]
            result = db.write_tx(update)
        """
        with self._lock:
            try:
                return self._run_tx(fn)
            except sqlite3.OperationalError as e:
                log.warning("SafeDB write_tx retry on %s: %s", self._db_path.name, e)
                return self._run_tx(fn)

    def _run_tx(self, fn):
        """Run fn in one IMMEDIATE transaction, rolling back on any error.

        Always leaves the connection with no open transaction — whether fn
        commits, raises IntegrityError, or the callback itself raises — so the
        write lock is never leaked. Must be called under ``self._lock``.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(self._conn)
            self._conn.execute("COMMIT")
            return result
        except Exception:
            self._rollback_safe()
            raise

    def _rollback_safe(self) -> None:
        """Rollback and verify connection is alive. Reconnect if dead."""
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._conn.execute("SELECT 1")
        except Exception:
            log.warning("SafeDB reconnecting: %s", self._db_path.name)
            self._connect()


def get_db(name: str) -> SafeDB:
    """Get or create a SafeDB instance by logical name.

    Per-agent databases resolve to ``./data/<agent_name>/<name>.db`` via
    ``settings.db_dir``. The special name ``"swarm"`` resolves to the shared
    cross-agent ledger at ``settings.swarm_db_path``.

    Known names:
      "session"         → session/conversation history (agent-private)
      "memory_fts"      → FTS5 fact index (agent-private)
      "knowledge_graph" → knowledge graph (agent-private)
      "mcp_registry"    → MCP registry (agent-private)
      "swarm"           → shared swarm ledger (all agents)
    """
    with _instances_lock:
        if name not in _instances:
            if name == "swarm":
                db_path = Path(settings.swarm_db_path)
            else:
                data_dir = Path(settings.db_dir)
                db_path = data_dir / f"{name}.db"
            _instances[name] = SafeDB(db_path)
        return _instances[name]
