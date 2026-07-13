"""SafeDB transaction safety.

A failed write must roll back and never leave the connection wedged in an
open IMMEDIATE transaction — otherwise every later write on that connection
deadlocks on the leaked write lock.
"""

import sqlite3

import pytest

from kronos.db import SafeDB


@pytest.fixture
def db(tmp_path):
    d = SafeDB(tmp_path / "t.db")
    d.write("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    return d


def test_write_tx_commits_on_success(db):
    db.write_tx(lambda c: c.execute("INSERT INTO t (id, v) VALUES (1, 'a')"))
    assert db.read_one("SELECT v FROM t WHERE id = 1")["v"] == "a"


def test_write_tx_rolls_back_on_callback_error(db):
    def _bad(conn):
        conn.execute("INSERT INTO t (id, v) VALUES (2, 'b')")
        raise ValueError("boom")

    with pytest.raises(ValueError):
        db.write_tx(_bad)

    # The insert was rolled back…
    assert db.read_one("SELECT v FROM t WHERE id = 2") is None
    # …and the connection is not wedged — a later write still commits.
    db.write("INSERT INTO t (id, v) VALUES (3, 'c')")
    assert db.read_one("SELECT v FROM t WHERE id = 3")["v"] == "c"


def test_write_rolls_back_on_integrity_error(db):
    db.write("INSERT INTO t (id, v) VALUES (1, 'a')")
    # Duplicate primary key → IntegrityError, NOT OperationalError, so the old
    # code left the BEGIN IMMEDIATE open.
    with pytest.raises(sqlite3.IntegrityError):
        db.write("INSERT INTO t (id, v) VALUES (1, 'dup')")
    # Connection still usable — the failed write rolled back, no open tx.
    db.write("INSERT INTO t (id, v) VALUES (4, 'd')")
    assert db.read_one("SELECT v FROM t WHERE id = 4")["v"] == "d"


def test_write_many_rolls_back_atomically_on_error(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.write_many(
            [
                ("INSERT INTO t (id, v) VALUES (5, 'e')", ()),
                ("INSERT INTO t (id, v) VALUES (5, 'dup')", ()),  # dup PK
            ]
        )
    # Neither row committed (atomic), and the connection is healthy.
    assert db.read_one("SELECT v FROM t WHERE id = 5") is None
    db.write("INSERT INTO t (id, v) VALUES (6, 'f')")
    assert db.read_one("SELECT v FROM t WHERE id = 6")["v"] == "f"
