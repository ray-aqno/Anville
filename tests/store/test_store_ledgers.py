"""Append-only ledgers (ADR 0006 §2). Schema beyond scope+payload+provenance waits for the harness RFC."""

import sqlite3
from contextlib import closing

import pytest


def test_entries_read_back_in_order_filtered_by_scope(store):
    store.append_ledger("constraint", "team:baja", {"rule": "min radius 3 mm"}, {"event": "comment:c1"})
    store.append_ledger("constraint", "part:upright", {"rule": "keep pocket"}, {"event": "comment:c2"})
    store.append_ledger("constraint", "team:baja", {"rule": "6061 only"}, {"event": "comment:c3"})

    entries = store.read_ledger("constraint", scope="team:baja")

    assert [e["payload"]["rule"] for e in entries] == ["min radius 3 mm", "6061 only"]
    assert entries[0]["provenance"] == {"event": "comment:c1"}


def test_ledgers_are_separate(store):
    store.append_ledger("shop_outcome", "shop:main", {"setups": 3}, {"event": "shop:n1"})

    assert store.read_ledger("constraint") == []
    assert len(store.read_ledger("shop_outcome")) == 1


def test_unknown_ledger_is_rejected(store):
    with pytest.raises(ValueError):
        store.append_ledger("rumours", "team:baja", {}, {"event": "x"})


def test_entry_without_provenance_is_rejected(store):
    with pytest.raises(ValueError):
        store.append_ledger("constraint", "team:baja", {"rule": "x"}, {})


@pytest.mark.parametrize("sql", ["UPDATE ledger SET scope = 'x'", "DELETE FROM ledger"])
def test_the_database_refuses_to_rewrite_history(store, db_path, sql):
    store.append_ledger("constraint", "team:baja", {"rule": "x"}, {"event": "comment:c1"})

    with closing(sqlite3.connect(db_path)) as conn, pytest.raises(sqlite3.DatabaseError):
        conn.execute(sql)
