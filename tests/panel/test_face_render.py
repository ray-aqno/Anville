"""The flagged face's picture (ADR 0007 Amendment 4): served to the panel by token and to the setup sheet by its
signed link. Main reads /data/render/{version_id}.png, so a version id is checked before it reaches the reader."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from anville.panel import make_sheet_router, sheet_sig
from tests.panel.conftest import DOC, SECRET, RecordingWriter, client_for, load
from tests.panel.test_traveler import BUY, SHEETS

PNG = b"\x89PNG\r\n\x1a\nfake"
VID = "v7aaaaaa0001"


def reader(store=None, calls=None):
    store = {VID: PNG} if store is None else store

    def read(version_id, document_id=None):
        if calls is not None:
            calls.append(version_id if document_id is None else (version_id, document_id))
        return store.get(version_id)
    return read


def panel(read_render=None, **kw):
    return client_for(load("part_a_live")["events"], **({"read_render": read_render} if read_render else {}), **kw)


def test_the_panel_gets_the_picture_by_token():
    r = panel(reader()).get(f"/panel/render/{VID}?documentId={DOC}&t={SECRET}")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"


def test_the_panel_asks_for_the_picture_of_its_own_document():
    """Codex, PR #51: main checks the version belongs to that document, so an allowed document can't fetch
    another document's render."""
    calls = []
    panel(reader(calls=calls)).get(f"/panel/render/{VID}?documentId={DOC}&t={SECRET}")
    assert calls == [(VID, DOC)]


def test_a_wrong_token_or_document_reads_nothing():
    calls = []
    c = panel(reader(calls=calls))
    assert c.get(f"/panel/render/{VID}?documentId={DOC}&t=wrong").status_code == 404
    assert c.get(f"/panel/render/{VID}?documentId=dOTHER&t={SECRET}").status_code == 404
    assert calls == []


def test_a_version_id_that_isnt_one_never_reaches_the_reader():
    calls = []
    c = panel(reader(calls=calls))
    for bad in ("v..x", "v.png", "a%2F..%2Fsecret", "x" * 65, "v%00"):
        assert c.get(f"/panel/render/{bad}?documentId={DOC}&t={SECRET}").status_code == 404, bad
    assert calls == []


def test_no_picture_yet_or_no_reader_is_404():
    assert panel(reader(store={})).get(f"/panel/render/{VID}?documentId={DOC}&t={SECRET}").status_code == 404
    assert panel().get(f"/panel/render/{VID}?documentId={DOC}&t={SECRET}").status_code == 404


def sheet_client(store=None, calls=None, sheets=None):
    with_vid = {sid: (doc, {**sheet, "version_id": VID}) for sid, (doc, sheet) in SHEETS.items()}
    app = FastAPI()
    app.include_router(make_sheet_router((sheets or with_vid).get, RecordingWriter(), panel_secret=SECRET,
                                         read_render=reader(store, calls)))
    return TestClient(app)


SID = BUY["sheet_id"]
SIG = sheet_sig(SECRET, SID)


def test_the_sheet_shows_the_flagged_face_when_its_picture_exists():
    html = sheet_client().get(f"/sheet/{SID}?s={SIG}").text
    assert f'<img src="/sheet/{SID}/render?s={SIG}"' in html
    assert html.index('class="face"') < html.index('class="setup"')  # the picture before the setups


def test_a_sheet_opened_before_sam_finishes_keeps_a_hidden_slot_that_sheet_js_fills():
    # Codex P2 (PR #51): the picture lands after the comment, so a sheet opened first never showed it.
    html = sheet_client(store={}).get(f"/sheet/{SID}?s={SIG}").text
    assert f'<figure class="face" hidden data-render="/sheet/{SID}/render?s={SIG}">' in html
    assert "<img " in html and "<img src=" not in html  # sheet.js sets src; no request until then
    assert html.index('class="face"') < html.index('class="setup"')


def test_no_slot_without_a_picture_route_or_a_version():
    assert 'class="face"' not in client_no_render().get(f"/sheet/{SID}?s={SIG}").text
    no_vid = {sid: (doc, sheet) for sid, (doc, sheet) in SHEETS.items()}
    assert 'class="face"' not in sheet_client(store={}, sheets=no_vid).get(f"/sheet/{SID}?s={SIG}").text


def client_no_render():
    with_vid = {sid: (doc, {**sheet, "version_id": VID}) for sid, (doc, sheet) in SHEETS.items()}
    app = FastAPI()
    app.include_router(make_sheet_router(with_vid.get, RecordingWriter(), panel_secret=SECRET))
    return TestClient(app)


def test_the_sheets_picture_comes_from_its_own_version():
    r = sheet_client().get(f"/sheet/{SID}/render?s={SIG}")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


def test_the_sheets_picture_needs_the_signature_and_a_real_version():
    calls = []
    assert sheet_client(calls=calls).get(f"/sheet/{SID}/render?s=bad").status_code == 404
    assert calls == []
    no_vid = {sid: (doc, sheet) for sid, (doc, sheet) in SHEETS.items()}  # a sheet stored before version_id
    assert sheet_client(calls=calls, sheets=no_vid).get(f"/sheet/{SID}/render?s={SIG}").status_code == 404
    odd = {SID: (DOC, {**SHEETS[SID][1], "version_id": "../facts/x"})}
    assert sheet_client(calls=calls, sheets=odd).get(f"/sheet/{SID}/render?s={SIG}").status_code == 404
    assert calls == []
