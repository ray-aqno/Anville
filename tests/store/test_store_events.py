"""ADR 0006 event lifecycle: received -> claimed -> done | failed."""

import sqlite3
import threading
import time

import pytest
from conftest import race, scalar

from anville.store import StaleClaim, Store

LEASE_S = 600  # Store default; matches handle_event's Modal timeout.
GRACE_S = 30


def test_twenty_concurrent_duplicate_deliveries_insert_one_row(store, db_path):
    inserted = race(20, lambda i: store.record("version:v1", "docA", {"delivery": i}))

    assert inserted.count(True) == 1
    assert scalar(db_path, "SELECT COUNT(*) FROM events") == 1


def test_twenty_concurrent_claims_grant_exactly_one(store):
    store.record("version:v1", "docA", {})

    tokens = race(20, lambda i: store.claim("version:v1"))

    assert [t for t in tokens if t is not None] == [1]


def test_redelivery_keeps_the_first_payload(store):
    store.record("version:v1", "docA", {"n": 1})
    store.record("version:v1", "docA", {"n": 2})

    assert store.event("version:v1").payload == {"n": 1}


def test_two_documents_are_claimed_and_finished_independently(store):
    store.record("version:a1", "docA", {})
    store.record("version:b1", "docB", {})

    tokens = race(2, lambda i: store.claim(["version:a1", "version:b1"][i]))
    store.finish("version:a1", tokens[0], ok=True)
    store.finish("version:b1", tokens[1], ok=False)

    assert tokens == [1, 1]
    assert store.event("version:a1").status == "done"
    assert store.event("version:b1").status == "failed"


def test_second_event_on_a_busy_document_waits_then_is_handed_off(store):
    store.record("version:v1", "docA", {})
    store.record("comment:c1", "docA", {})
    token = store.claim("version:v1")

    assert store.claim("comment:c1") is None
    assert store.finish("version:v1", token, ok=True) == "comment:c1"
    assert store.claim("comment:c1") == 1


def test_finish_with_nothing_waiting_hands_off_nothing(store):
    store.record("version:v1", "docA", {})
    store.record("version:b1", "docB", {})

    assert store.finish("version:v1", store.claim("version:v1"), ok=True) is None


def test_finished_event_cannot_be_claimed_again(store):
    store.record("version:v1", "docA", {})
    store.finish("version:v1", store.claim("version:v1"), ok=True)

    assert store.claim("version:v1") is None


def test_unknown_key_cannot_be_claimed(store):
    assert store.claim("version:nope") is None


def test_killed_job_is_reaped_only_after_its_lease(store, clock):
    store.record("version:v1", "docA", {"x": 1})
    store.claim("version:v1")  # the job dies here and never finishes

    clock.advance(LEASE_S - 1)
    assert store.reapable(GRACE_S) == []

    clock.advance(2)
    reaped = store.reapable(GRACE_S)
    assert [(e.key, e.payload) for e in reaped] == [("version:v1", {"x": 1})]
    assert store.claim("version:v1") == 2


def test_finish_from_a_superseded_claim_is_rejected(store, clock):
    store.record("version:v1", "docA", {})
    slow = store.claim("version:v1")
    clock.advance(LEASE_S + 1)
    fresh = store.claim("version:v1")

    with pytest.raises(StaleClaim):
        store.finish("version:v1", slow, ok=True)
    assert store.event("version:v1").status == "claimed"

    store.finish("version:v1", fresh, ok=True)
    assert store.event("version:v1").status == "done"


def test_event_never_spawned_is_reaped_after_the_grace_period(store, clock):
    store.record("version:v1", "docA", {})

    assert store.reapable(GRACE_S) == []
    clock.advance(GRACE_S + 1)
    assert [e.key for e in store.reapable(GRACE_S)] == ["version:v1"]


def test_reaper_skips_documents_with_a_live_claim(store, clock):
    store.record("version:v1", "docA", {})
    store.record("comment:c1", "docA", {})
    store.claim("version:v1")
    clock.advance(GRACE_S + 1)

    assert store.reapable(GRACE_S) == []


def test_reaper_returns_only_the_oldest_waiting_event_per_document(store, clock):
    store.record("comment:c1", "docA", {})
    clock.advance(1)
    store.record("comment:c2", "docA", {})
    store.record("comment:b1", "docB", {})
    clock.advance(GRACE_S + 1)

    assert sorted(e.key for e in store.reapable(GRACE_S)) == ["comment:b1", "comment:c1"]


def test_event_out_of_attempts_is_marked_failed_not_reaped(store, clock):
    store.record("version:v1", "docA", {})
    for _ in range(3):
        assert store.claim("version:v1") is not None
        clock.advance(LEASE_S + 1)

    assert store.reapable(GRACE_S) == []
    assert store.event("version:v1").status == "failed"


def test_after_commit_runs_only_for_event_deliveries_and_ledger_entries(db_path, clock):
    # volume.commit() costs ~1.5 s on Modal (probe c8f35196), so only writes whose loss
    # matters pay it; the rest ride Modal's background commits.
    commits = []
    s = Store(db_path, clock=clock, after_commit=lambda: commits.append(1))

    s.record("version:v1", "docA", {})                   # durable
    s.record("version:v1", "docA", {})                   # duplicate: durable too (see below)
    token = s.claim("version:v1")
    s.reserve_post("version:v1", "review")
    s.complete_post("version:v1", "review", "c1")
    s.append_trace("docA", "r1", 0, "run_started", "start")
    s.reserve_call("2026-10-27", cap=10)
    s.release_call("2026-10-27")
    s.reapable(30)
    s.finish("version:v1", token, ok=True)
    s.append_ledger("constraint", "team:baja", {"rule": "x"}, {"event": "comment:c1"})  # durable
    s.close()

    assert len(commits) == 3


def test_duplicate_delivery_retries_a_durable_save_that_failed(db_path, clock):
    # Greptile P1 on PR #4: the row is in SQLite but the Volume save failed. A redelivery
    # must not answer "duplicate" (-> 200) without making the first insert durable.
    outcomes = iter([OSError("volume busy"), None])

    def flaky_commit():
        err = next(outcomes)
        if err:
            raise err

    s = Store(db_path, clock=clock, after_commit=flaky_commit)
    with pytest.raises(OSError):
        s.record("version:v1", "docA", {})

    assert s.record("version:v1", "docA", {}) is False  # no exception: the save went through
    assert next(outcomes, "exhausted") == "exhausted"
    s.close()


def test_duplicate_delivery_waits_for_the_first_durable_save(db_path, clock):
    # Greptile P1 on PR #4: a concurrent duplicate must not return while the first
    # delivery's volume.commit() is still running.
    in_commit, release, calls = threading.Event(), threading.Event(), []

    def slow_first_commit():
        calls.append(1)
        if len(calls) == 1:
            in_commit.set()
            release.wait(5)

    s = Store(db_path, clock=clock, after_commit=slow_first_commit)
    first = threading.Thread(target=s.record, args=("version:v1", "docA", {}))
    first.start()
    assert in_commit.wait(5)

    dup_done = threading.Event()
    dup = threading.Thread(target=lambda: (s.record("version:v1", "docA", {}), dup_done.set()))
    dup.start()
    assert not dup_done.wait(0.3)  # still blocked behind the first save

    release.set()
    first.join(5)
    dup.join(5)
    assert dup_done.is_set()
    s.close()


def test_burst_of_deliveries_shares_durable_saves(db_path, clock):
    # Staging (PR #7): 20 concurrent duplicates took p50 12.2 s at /hook, because every
    # record() ran its own volume.commit() in a row. With group commit a burst costs ~2 saves,
    # and each record() still returns only after a save that started after its row committed.
    saves, durable = [], set()

    def slow_save():
        conn = sqlite3.connect(db_path)
        seen = {r[0] for r in conn.execute("SELECT key FROM events")}  # what this save captures
        conn.close()
        time.sleep(0.2)
        saves.append(1)
        durable.update(seen)

    s = Store(db_path, clock=clock, after_commit=slow_save)
    returned_unsaved = []

    def deliver(key):
        s.record(key, "docA", {})
        if key not in durable:
            returned_unsaved.append(key)

    keys = ["comment:c1"] * 10 + [f"comment:c{i}" for i in range(2, 12)]
    threads = [threading.Thread(target=deliver, args=(k,)) for k in keys]
    t0 = time.monotonic()
    [t.start() for t in threads]
    [t.join(10) for t in threads]

    assert returned_unsaved == []
    assert len(saves) <= 3
    assert time.monotonic() - t0 < 2  # 20 saves in a row would take 4 s
    s.close()


def test_database_uses_wal(db_path, store):
    store.record("version:v1", "docA", {})

    assert scalar(db_path, "PRAGMA journal_mode") == "wal"
