"""The panel router: token and allowlist auth, the page, its assets, and /api/runs (ADR 0007 + Amendment 2)."""

from tests.panel.conftest import DOC, SECRET, client_for


def ev(cursor: int, run_id: str) -> dict:
    return {"cursor": cursor, "run_id": run_id, "seq": 1, "ts": float(cursor), "document_id": DOC,
            "version_id": None, "element_id": None, "kind": "tool_call", "title": "x", "detail": {}, "face_ids": []}


def test_panel_page_is_served_with_token_and_allowlisted_doc(client):
    r = client.get(f"/panel?documentId={DOC}&t={SECRET}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_bad_token_or_doc_is_an_empty_404(client):
    for url in (f"/panel?documentId={DOC}&t=wrong", f"/panel?documentId={DOC}", f"/panel?documentId=dOTHER&t={SECRET}",
                f"/panel?t={SECRET}", f"/api/runs?documentId={DOC}&t=wrong", f"/api/runs?documentId=dOTHER&t={SECRET}",
                f"/api/runs?documentId={DOC}&t=%C3%A9"):
        r = client.get(url)
        assert (r.status_code, r.content) == (404, b""), url


def test_assets_are_served_from_an_allowlist_without_token(client):
    r = client.get("/panel/assets/panel.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert client.get("/panel/assets/panel.css").headers["content-type"].startswith("text/css")
    for name in ("../router.py", "..%2Frouter.py", "secret.txt", "DESIGN.md"):
        assert client.get(f"/panel/assets/{name}").status_code == 404, name


def test_runs_groups_events_by_run_oldest_first_with_cursor(fallback):
    body = client_for(fallback["events"]).get(f"/api/runs?documentId={DOC}&t={SECRET}").json()
    assert [r["run_id"] for r in body["runs"]] == ["v6bbbbbb-1", "v7aaaaaa-1"]
    assert body["cursor"] == max(e["cursor"] for e in fallback["events"])
    seen = [e["cursor"] for r in body["runs"] for e in r["events"]]
    assert seen == sorted(seen) and len(seen) == len(fallback["events"])


def test_runs_pages_past_the_first_page_and_keeps_the_latest_five():
    events = [ev(i + 1, f"r{i // 100:02d}") for i in range(1200)]  # 12 runs of 100 events
    body = client_for(events).get(f"/api/runs?documentId={DOC}&t={SECRET}").json()
    assert [r["run_id"] for r in body["runs"]] == ["r07", "r08", "r09", "r10", "r11"]
    assert body["cursor"] == 1200


def test_runs_on_an_empty_document():
    assert client_for([]).get(f"/api/runs?documentId={DOC}&t={SECRET}").json() == {"runs": [], "cursor": 0}


def test_runs_reaches_the_latest_runs_on_a_long_document(monkeypatch):
    # codex P2: paging used to stop at 40 pages and return the oldest runs. 120 pages here.
    import anville.panel.router as router
    monkeypatch.setattr(router, "PAGE", 10)
    events = [ev(i + 1, f"r{i // 100:02d}") for i in range(1200)]
    body = client_for(events).get(f"/api/runs?documentId={DOC}&t={SECRET}").json()
    assert [r["run_id"] for r in body["runs"]] == ["r07", "r08", "r09", "r10", "r11"]
    assert body["cursor"] == 1200
    assert all(len(r["events"]) == 100 for r in body["runs"])
