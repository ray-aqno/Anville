"""POST /api/ask: the designer's selection + question as an idempotent panel.ask event (ADR 0007 + Amendment 2 §3)."""

import uuid

from tests.panel.conftest import DOC, SECRET, RecordingWriter, client_for


def body(**over):
    b = {"request_id": str(uuid.uuid4()), "documentId": DOC, "workspaceId": "wSYN", "elementId": "eSYN",
         "face_ids": ["JLG"], "question": "Can this pocket go deeper without another setup?"}
    b.update(over)
    return b


def test_first_ask_is_recorded_as_a_panel_ask_event():
    writer = RecordingWriter()
    b = body()
    r = client_for([], writer=writer, clock=lambda: 1793300000.0).post(f"/api/ask?t={SECRET}", json=b)
    assert r.status_code == 202
    assert r.json() == {"event_id": f"ask:{b['request_id']}"}
    doc, payload = writer.rows[f"ask:{b['request_id']}"]
    assert doc == DOC
    assert payload == {**b, "event": "panel.ask", "microversion_id": None, "received_at": 1793300000.0}


def test_a_retried_ask_returns_the_original_event_id():
    writer, b = RecordingWriter(), body()
    c = client_for([], writer=writer)
    assert c.post(f"/api/ask?t={SECRET}", json=b).status_code == 202
    r = c.post(f"/api/ask?t={SECRET}", json=b)
    assert (r.status_code, r.json()) == (200, {"event_id": f"ask:{b['request_id']}"})
    assert len(writer.rows) == 1


def test_microversion_is_kept_when_the_client_sends_it():
    writer, b = RecordingWriter(), body(microversion_id="mSYN")
    client_for([], writer=writer).post(f"/api/ask?t={SECRET}", json=b)
    assert writer.rows[f"ask:{b['request_id']}"][1]["microversion_id"] == "mSYN"


def test_an_empty_question_is_allowed():
    assert client_for([]).post(f"/api/ask?t={SECRET}", json=body(question="")).status_code == 202


def test_invalid_asks_are_422():
    c = client_for([])
    for bad in (body(request_id="not-a-uuid"), body(face_ids=[f"F{i}" for i in range(21)]),
                body(face_ids=["x" * 65]), body(question="q" * 501), body(workspaceId=""),
                {k: v for k, v in body().items() if k != "elementId"}):
        assert c.post(f"/api/ask?t={SECRET}", json=bad).status_code == 422, bad


def test_ask_auth_is_an_empty_404():
    writer = RecordingWriter()
    c = client_for([], writer=writer)
    for url, b in ((f"/api/ask?t=bad", body()), ("/api/ask", body()), (f"/api/ask?t={SECRET}", body(documentId="dOTHER"))):
        r = c.post(url, json=b)
        assert (r.status_code, r.content) == (404, b""), url
    assert writer.rows == {}


def test_a_bad_token_with_a_bad_body_is_still_404_not_422():
    assert client_for([]).post("/api/ask?t=bad", json={"documentId": DOC, "request_id": "x"}).status_code == 404
    assert client_for([]).post("/api/ask?t=bad", content=b"not json").status_code == 404


def test_without_a_writer_asks_are_refused_not_accepted():
    """Codex, PR #34 (P1): mounted with no panel.ask handler, a 202 promised an answer nobody would give."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from anville.panel import make_router
    from tests.panel.conftest import DOC, SECRET, list_reader

    app = FastAPI()
    app.include_router(make_router(list_reader([]), None, panel_secret=SECRET, allowed_docs=lambda: {DOC}))
    c = TestClient(app)
    assert c.post(f"/api/ask?t={SECRET}", json=body()).status_code == 501
    assert c.post("/api/ask?t=wrong", json=body()).status_code == 404  # auth still first
