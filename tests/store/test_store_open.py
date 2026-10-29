"""Opening the store while another process holds a write lock (the flaky 20-process test).

Measured 2026-10-29: SQLite fails a contended `PRAGMA journal_mode=WAL` at once with "database is
locked", without calling the busy handler, so busy_timeout never covers it. Twenty cold processes
opening one fresh database hit that about one run in three on a loaded machine.
"""

import sqlite3
import threading

from anville.store import Store


def test_the_store_opens_once_a_competing_write_lock_is_released(db_path):
    other = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)  # a Timer releases it
    other.execute("CREATE TABLE warmup (x)")
    other.execute("BEGIN IMMEDIATE")  # holds the write lock; the file is still in rollback-journal mode
    threading.Timer(0.3, lambda: other.execute("COMMIT")).start()

    s = Store(db_path, busy_timeout_ms=5_000)
    assert s._conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.close()
    other.close()
