"""Durable event store (ADR 0006): SQLite, WAL, explicit commits.

Every write runs in its own `BEGIN IMMEDIATE` transaction, so concurrent
writers queue on SQLite's write lock (up to busy_timeout) instead of failing
mid-transaction. On Modal, exactly one container may open the file (the
single-writer rule); locally, any number of threads or processes may.

Stdlib only, so it runs unchanged locally and inside Modal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    key          TEXT PRIMARY KEY,              -- idempotency key: version:<vid>, comment:<cid>, ask:<rid>
    document_id  TEXT NOT NULL,
    payload      TEXT NOT NULL,                 -- the first delivery's JSON; redeliveries are ignored
    status       TEXT NOT NULL DEFAULT 'received'
                 CHECK (status IN ('received', 'claimed', 'done', 'failed')),
    received_at  REAL NOT NULL,
    claimed_at   REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,    -- also the fencing token of the current claim
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS events_by_doc ON events (document_id, status);

CREATE TABLE IF NOT EXISTS doc_state (
    document_id  TEXT PRIMARY KEY,
    version_id   TEXT NOT NULL,                 -- latest version under review
    version_ts   REAL NOT NULL                  -- Onshape's event timestamp, else arrival time
);

CREATE TABLE IF NOT EXISTS posted (
    event_key    TEXT NOT NULL,                 -- the event that produced the comment
    kind         TEXT NOT NULL,                 -- review, setup_sheet, answer, ...
    reserved_at  REAL NOT NULL,
    comment_id   TEXT,                          -- NULL until Onshape confirms the post
    posted_at    REAL,
    PRIMARY KEY (event_key, kind)
);

-- ADR 0007 trace events. AUTOINCREMENT so a cursor is never reused, even if rows are pruned.
CREATE TABLE IF NOT EXISTS trace (
    cursor       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           REAL NOT NULL,
    version_id   TEXT,
    element_id   TEXT,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL,                 -- JSON object
    face_ids     TEXT NOT NULL                  -- JSON array
);
CREATE INDEX IF NOT EXISTS trace_by_doc ON trace (document_id, cursor);

-- Constraint and shop-outcome ledgers. Minimal on purpose: scope rules, conflict
-- handling and forgetting are open (CLAUDE.md §4, harness schema) and need an RFC.
CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger       TEXT NOT NULL CHECK (ledger IN ('constraint', 'shop_outcome')),
    scope        TEXT NOT NULL,                 -- part:<id>, team:<id>, shop:<id>
    payload      TEXT NOT NULL,                 -- JSON
    provenance   TEXT NOT NULL,                 -- JSON: which event/run/verification produced it
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_by_scope ON ledger (ledger, scope, id);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
    BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
    BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

-- Onshape calls per UTC day, against our own cap (OnshapeClient's CallLedger).
CREATE TABLE IF NOT EXISTS call_budget (
    day          TEXT PRIMARY KEY,              -- YYYY-MM-DD, UTC
    n            INTEGER NOT NULL DEFAULT 0
);
"""

LEDGERS = frozenset({"constraint", "shop_outcome"})

TRACE_KINDS = frozenset({
    "run_started", "tool_call", "tool_result", "option", "decision",
    "action", "verify", "comment_posted", "run_finished", "error",
})


class StaleClaim(Exception):
    """finish() came from a claim that has since been superseded (lease expired, event re-claimed)."""


@dataclass(frozen=True)
class Event:
    key: str
    document_id: str
    payload: dict
    status: str
    attempts: int
    received_at: float
    claimed_at: float | None


class Store:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        after_commit: Callable[[], None] | None = None,
        lease_s: float = 600,
        max_attempts: int = 3,
        busy_timeout_ms: int = 10_000,
    ):
        self.path = Path(path)
        self._clock = clock
        # On Modal this is volume.commit(): SQLite's commit alone doesn't make the write durable.
        # It costs ~1.5 s there (probe c8f35196), so only writes whose loss matters call it:
        # an event delivery (it gates the webhook's 200) and a ledger entry (a verified lesson).
        # Everything else rides Modal's background commits and the commit at container shutdown.
        self._after_commit = after_commit
        # Group commit (see _persist): writes counted after their SQLite COMMIT, and how many
        # of them a finished after_commit has covered.
        self._persist_cv = threading.Condition()
        self._written = 0
        self._durable = 0
        self._persisting = False
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(f"BEGIN IMMEDIATE; {SCHEMA} COMMIT;")

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._conns:
                conn.close()
            self._conns.clear()

    # --- events -------------------------------------------------------------

    def record(
        self,
        key: str,
        document_id: str,
        payload: dict,
        version_id: str | None = None,
        version_ts: float | None = None,
    ) -> bool:
        """Insert a delivery; False means the key was already recorded (a duplicate).

        A version event (`version_id` given) also advances doc_state in the same
        transaction, if it is newer than the version already there. Webhooks can
        arrive out of order, so "newer" is `version_ts` (Onshape's event
        timestamp), falling back to arrival time.

        Returns only once the event is durable, duplicates included: a duplicate
        waits for the first delivery's save, and retries it if that save failed.
        Otherwise a redelivery could be acknowledged for an event not yet saved.
        """
        now = self._clock()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events (key, document_id, payload, received_at) VALUES (?, ?, ?, ?)",
                (key, document_id, json.dumps(payload, sort_keys=True), now),
            )
            inserted = cur.rowcount == 1
            if inserted and version_id is not None:
                conn.execute(
                    """
                    INSERT INTO doc_state (document_id, version_id, version_ts) VALUES (?, ?, ?)
                    ON CONFLICT (document_id) DO UPDATE
                        SET version_id = excluded.version_id, version_ts = excluded.version_ts
                        WHERE excluded.version_ts > doc_state.version_ts
                    """,
                    (document_id, version_id, now if version_ts is None else version_ts),
                )
        self._persist()
        return inserted

    def claim(self, key: str) -> int | None:
        """Take the event for processing; returns a fencing token, or None if it isn't ours to run.

        Succeeds for a `received` event, or a `claimed` one whose lease expired,
        while attempts remain and no other event on the same document holds a
        live claim (ADR 0003: one loop per document at a time).
        """
        now = self._clock()
        with self._write() as conn:
            row = conn.execute(
                """
                UPDATE events SET status = 'claimed', claimed_at = :now, attempts = attempts + 1
                WHERE key = :key
                  AND attempts < :max_attempts
                  AND (status = 'received' OR (status = 'claimed' AND claimed_at <= :expired))
                  AND NOT EXISTS (
                      SELECT 1 FROM events o
                      WHERE o.document_id = events.document_id AND o.key != events.key
                        AND o.status = 'claimed' AND o.claimed_at > :expired)
                RETURNING attempts
                """,
                {"key": key, "now": now, "expired": now - self._lease_s,
                 "max_attempts": self._max_attempts},
            ).fetchone()
            return row[0] if row else None

    def finish(self, key: str, token: int, ok: bool) -> str | None:
        """Close the claim; returns the next waiting event key on the same document, if any.

        Raises StaleClaim if `token` is no longer the current claim, so a job
        that outlived its lease learns it lost ownership.
        """
        with self._write() as conn:
            row = conn.execute(
                """
                UPDATE events SET status = ?, finished_at = ?
                WHERE key = ? AND status = 'claimed' AND attempts = ?
                RETURNING document_id
                """,
                ("done" if ok else "failed", self._clock(), key, token),
            ).fetchone()
            if row is None:
                raise StaleClaim(f"{key}: claim {token} is no longer current")
            nxt = conn.execute(
                "SELECT key FROM events WHERE document_id = ? AND status = 'received' "
                "ORDER BY received_at, key LIMIT 1",
                (row[0],),
            ).fetchone()
            return nxt[0] if nxt else None

    def reapable(self, received_grace_s: float) -> list[Event]:
        """Events to re-spawn: `received` past the grace period, or `claimed` past the lease.

        Returns at most one event per document, and none for a document with a
        live claim. Events that have used every attempt are marked `failed`
        here, so they stop coming back.
        """
        now = self._clock()
        expired = now - self._lease_s
        with self._write() as conn:
            conn.execute(
                "UPDATE events SET status = 'failed', finished_at = ? "
                "WHERE status = 'claimed' AND claimed_at <= ? AND attempts >= ?",
                (now, expired, self._max_attempts),
            )
            rows = conn.execute(
                """
                SELECT * FROM events e
                WHERE ((e.status = 'received' AND e.received_at <= :received_before)
                    OR (e.status = 'claimed' AND e.claimed_at <= :expired))
                  AND NOT EXISTS (
                      SELECT 1 FROM events o
                      WHERE o.document_id = e.document_id
                        AND o.status = 'claimed' AND o.claimed_at > :expired)
                ORDER BY e.received_at, e.key
                """,
                {"received_before": now - received_grace_s, "expired": expired},
            ).fetchall()
        oldest: dict[str, Event] = {}
        for r in rows:
            oldest.setdefault(r["document_id"], _event(r))
        return list(oldest.values())

    def is_current(self, document_id: str, version_id: str) -> bool:
        """False if a newer version of the document has been recorded since.

        Check-then-post is not atomic: a version recorded between this check and
        the Onshape call still gets the stale comment. The window is one HTTP
        call long; holding a lock across that call would be worse.
        """
        row = self._conn().execute(
            "SELECT version_id FROM doc_state WHERE document_id = ?", (document_id,)
        ).fetchone()
        return row is None or row[0] == version_id

    # --- posted comments ----------------------------------------------------

    def reserve_post(self, event_key: str, kind: str) -> bool:
        """Claim the right to post; call before the Onshape request. False means don't post.

        Reserving first makes posting at-most-once: a crash between the post and
        complete_post() leaves an unconfirmed row instead of a second comment.
        """
        with self._write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO posted (event_key, kind, reserved_at) VALUES (?, ?, ?)",
                (event_key, kind, self._clock()),
            )
            return cur.rowcount == 1

    def complete_post(self, event_key: str, kind: str, comment_id: str) -> None:
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE posted SET comment_id = ?, posted_at = ? WHERE event_key = ? AND kind = ?",
                (comment_id, self._clock(), event_key, kind),
            )
            if cur.rowcount != 1:
                raise KeyError(f"no reserved post for {event_key} / {kind}")

    def already_posted(self, event_key: str, kind: str) -> bool:
        """True once reserved, confirmed or not."""
        row = self._conn().execute(
            "SELECT 1 FROM posted WHERE event_key = ? AND kind = ?", (event_key, kind)
        ).fetchone()
        return row is not None

    def unconfirmed_posts(self, older_than_s: float) -> list[tuple[str, str]]:
        """Reservations never confirmed: the job died mid-post. For the reaper to flag, not repost."""
        rows = self._conn().execute(
            "SELECT event_key, kind FROM posted WHERE comment_id IS NULL AND reserved_at <= ? "
            "ORDER BY reserved_at, event_key",
            (self._clock() - older_than_s,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # --- trace (ADR 0007) ---------------------------------------------------

    def append_trace(
        self,
        document_id: str,
        run_id: str,
        seq: int,
        kind: str,
        title: str,
        *,
        ts: float | None = None,
        version_id: str | None = None,
        element_id: str | None = None,
        detail: dict | None = None,
        face_ids: list[str] | tuple[str, ...] = (),
    ) -> int:
        """Append one trace event; returns its cursor.

        Cursors are assigned under SQLite's write lock and commit in that order,
        so a reader replaying `after=<cursor>` never skips a late-committing row.
        """
        if kind not in TRACE_KINDS:
            raise ValueError(f"unknown trace kind {kind!r}")
        if face_ids and not element_id:
            raise ValueError("face_ids need element_id (ADR 0007)")
        with self._write() as conn:
            cur = conn.execute(
                """
                INSERT INTO trace (document_id, run_id, seq, ts, version_id, element_id,
                                   kind, title, detail, face_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, run_id, seq, self._clock() if ts is None else ts, version_id,
                 element_id, kind, title, json.dumps(detail or {}), json.dumps(list(face_ids))),
            )
            return cur.lastrowid

    def trace_after(self, document_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        """Trace events for a document with cursor > after, oldest first (the panel's replay)."""
        rows = self._conn().execute(
            "SELECT * FROM trace WHERE document_id = ? AND cursor > ? ORDER BY cursor LIMIT ?",
            (document_id, after, limit),
        ).fetchall()
        return [
            {
                "cursor": r["cursor"], "run_id": r["run_id"], "seq": r["seq"], "ts": r["ts"],
                "document_id": r["document_id"], "version_id": r["version_id"],
                "element_id": r["element_id"], "kind": r["kind"], "title": r["title"],
                "detail": json.loads(r["detail"]), "face_ids": json.loads(r["face_ids"]),
            }
            for r in rows
        ]

    # --- ledgers -------------------------------------------------------------

    def append_ledger(self, ledger: str, scope: str, payload: dict, provenance: dict) -> int:
        """Append a verified lesson. Write only after the planner has verified it (ADR 0006 §2)."""
        if ledger not in LEDGERS:
            raise ValueError(f"unknown ledger {ledger!r}")
        if not provenance:
            raise ValueError("ledger entries need provenance")
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO ledger (ledger, scope, payload, provenance, created_at) VALUES (?, ?, ?, ?, ?)",
                (ledger, scope, json.dumps(payload), json.dumps(provenance), self._clock()),
            )
        self._persist()
        return cur.lastrowid

    def read_ledger(self, ledger: str, scope: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM ledger WHERE ledger = ?", [ledger]
        if scope is not None:
            sql, args = sql + " AND scope = ?", args + [scope]
        rows = self._conn().execute(sql + " ORDER BY id", args).fetchall()
        return [
            {"id": r["id"], "ledger": r["ledger"], "scope": r["scope"], "created_at": r["created_at"],
             "payload": json.loads(r["payload"]), "provenance": json.loads(r["provenance"])}
            for r in rows
        ]

    # --- Onshape call budget (OnshapeClient's CallLedger) -------------------

    def reserve_call(self, day: str, cap: int) -> bool:
        """Take one call from the day's budget; False if the cap is reached.

        Check and increment are one UPDATE, so racing jobs can't both take the last slot.
        """
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO call_budget (day) VALUES (?)", (day,))
            cur = conn.execute("UPDATE call_budget SET n = n + 1 WHERE day = ? AND n < ?", (day, cap))
            return cur.rowcount == 1

    def release_call(self, day: str) -> None:
        """Hand back a reservation for a call Onshape didn't bill."""
        with self._write() as conn:
            conn.execute("UPDATE call_budget SET n = n - 1 WHERE day = ? AND n > 0", (day,))

    def calls(self, day: str) -> int:
        row = self._conn().execute("SELECT n FROM call_budget WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0

    def event(self, key: str) -> Event | None:
        row = self._conn().execute("SELECT * FROM events WHERE key = ?", (key,)).fetchone()
        return _event(row) if row else None

    # --- plumbing -----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # One connection per thread; isolation_level=None so BEGIN/COMMIT are ours alone.
            # check_same_thread=False only so close() can run from another thread.
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=FULL")  # WAL's default NORMAL can drop the last commits
            with self._conns_lock:
                self._conns.append(conn)
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _persist(self) -> None:
        """Return once every write committed before this call is durable.

        Group commit: one after_commit runs at a time, and it covers every write counted
        before it started. Callers already covered by a running save just wait for it, so
        a burst of N deliveries costs about 2 saves instead of N in a row (12 s for 20
        duplicates on staging before this). A write is counted only after its SQLite
        COMMIT, so a save that starts later always includes it. A failed save covers
        nothing: the error reaches its caller, and a waiter retries.
        """
        if self._after_commit is None:
            return
        cv = self._persist_cv
        with cv:
            self._written += 1
            mine = self._written
            while self._durable < mine:
                if self._persisting:
                    cv.wait()
                    continue
                self._persisting, covers, saved = True, self._written, False
                cv.release()
                try:
                    self._after_commit()
                    saved = True
                finally:
                    cv.acquire()
                    self._persisting = False
                    if saved:
                        self._durable = max(self._durable, covers)
                    cv.notify_all()


def _event(row: sqlite3.Row) -> Event:
    return Event(
        key=row["key"],
        document_id=row["document_id"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        attempts=row["attempts"],
        received_at=row["received_at"],
        claimed_at=row["claimed_at"],
    )
