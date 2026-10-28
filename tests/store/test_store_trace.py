"""ADR 0007 trace events: a cursor monotonic across runs, replayed by after=<cursor>."""

import pytest
from conftest import race


def _emit(store, doc="docA", run="r1", seq=0, kind="tool_call", **kw):
    return store.append_trace(doc, run, seq, kind, f"{run}#{seq}", **kw)


def test_replay_after_a_cursor_returns_later_events_across_runs(store):
    c1 = _emit(store, run="r1", seq=0)
    c2 = _emit(store, run="r1", seq=1)
    c3 = _emit(store, run="r2", seq=0)

    assert c1 < c2 < c3
    assert [e["title"] for e in store.trace_after("docA", c1)] == ["r1#1", "r2#0"]
    assert store.trace_after("docA", c3) == []


def test_replay_is_scoped_to_one_document(store):
    _emit(store, doc="docA", run="ra")
    _emit(store, doc="docB", run="rb")

    assert [e["run_id"] for e in store.trace_after("docA", 0)] == ["ra"]


def test_trace_event_has_the_adr_0007_shape(store, clock):
    cursor = store.append_trace(
        "docA", "r_8f2c", 7, "verify", "Re-check: 3 -> 2 setups",
        version_id="v1", element_id="e1", detail={"before": 3, "after": 2}, face_ids=["JLG"],
    )

    assert store.trace_after("docA", 0) == [{
        "cursor": cursor,
        "run_id": "r_8f2c",
        "seq": 7,
        "ts": clock.t,
        "document_id": "docA",
        "version_id": "v1",
        "element_id": "e1",
        "kind": "verify",
        "title": "Re-check: 3 -> 2 setups",
        "detail": {"before": 3, "after": 2},
        "face_ids": ["JLG"],
    }]


def test_face_ids_without_an_element_are_rejected(store):
    # ADR 0007: a document can hold several Part Studios, so faces need their element.
    with pytest.raises(ValueError):
        _emit(store, face_ids=["JLG"])


def test_unknown_kind_is_rejected(store):
    with pytest.raises(ValueError):
        _emit(store, kind="chat")


def test_concurrent_appends_get_distinct_cursors_and_all_replay(store):
    cursors = race(20, lambda i: _emit(store, run=f"r{i}"))

    replayed = [e["cursor"] for e in store.trace_after("docA", 0)]
    assert len(set(cursors)) == 20
    assert replayed == sorted(cursors)


def test_replay_pages_with_a_limit(store):
    for i in range(5):
        _emit(store, seq=i)

    first = store.trace_after("docA", 0, limit=2)
    rest = store.trace_after("docA", first[-1]["cursor"], limit=10)

    assert [e["seq"] for e in first + rest] == [0, 1, 2, 3, 4]
