"""GET /api/stream: server-sent trace events, resumed by cursor (ADR 0007 + Amendment 2 §3)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from anville.panel import make_router
from tests.panel.conftest import DOC, SECRET, RecordingWriter, client_for

FAST = {"poll_s": 0.01, "heartbeat_s": 0.05, "stream_s": 0.3}


def frames(raw: str) -> list[dict]:
    """Parse an SSE body into frames: {field: value}, with comment lines under ':'."""
    out = []
    for block in raw.split("\n\n"):
        f: dict = {}
        for line in block.splitlines():
            field, _, value = line.partition(":")
            f[field] = value.lstrip(" ") if field else value.strip()
        if f:
            out.append(f)
    return out


def trace_ids(raw: str) -> list[int]:
    return [int(f["id"]) for f in frames(raw) if f.get("event") == "trace"]


def test_stream_sends_events_after_the_cursor_with_their_ids(live):
    r = client_for(live["events"], **FAST).get(f"/api/stream?documentId={DOC}&after=3&t={SECRET}")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert frames(r.text)[0].get("retry") == "1000"
    assert trace_ids(r.text) == [e["cursor"] for e in live["events"] if e["cursor"] > 3]


def test_last_event_id_wins_when_greater_than_after(live):
    # EventSource reconnects with its original URL (after=0) and sends Last-Event-ID.
    r = client_for(live["events"], **FAST).get(f"/api/stream?documentId={DOC}&after=0&t={SECRET}",
                                               headers={"Last-Event-ID": "10"})
    assert trace_ids(r.text)[0] == 11


def test_a_bad_last_event_id_falls_back_to_after(live):
    r = client_for(live["events"], **FAST).get(f"/api/stream?documentId={DOC}&after=20&t={SECRET}",
                                               headers={"Last-Event-ID": "nope"})
    assert trace_ids(r.text)[0] == 21


def test_stream_sends_heartbeats_and_closes_on_its_own():
    r = client_for([], **FAST).get(f"/api/stream?documentId={DOC}&after=0&t={SECRET}")
    assert r.status_code == 200
    assert any(f.get("") == "hb" for f in frames(r.text))


def test_stream_picks_up_events_appended_while_open(live):
    events, calls = list(live["events"][:5]), []
    late = live["events"][5]

    def reader(document_id, after, limit):
        calls.append(after)
        if len(calls) == 3:
            events.append(late)  # the job appends while the panel is watching
        return [e for e in events if e["cursor"] > after][:limit]

    app = FastAPI()
    app.include_router(make_router(reader, RecordingWriter(), panel_secret=SECRET, allowed_docs=lambda: {DOC}, **FAST))
    r = TestClient(app).get(f"/api/stream?documentId={DOC}&after=0&t={SECRET}")
    assert trace_ids(r.text) == [1, 2, 3, 4, 5, late["cursor"]]


def test_stream_auth(live):
    c = client_for(live["events"], **FAST)
    for url in (f"/api/stream?documentId={DOC}&t=bad", f"/api/stream?documentId=dOTHER&t={SECRET}"):
        r = c.get(url)
        assert (r.status_code, r.content) == (404, b""), url
