"""The traveler (P2): GET /sheet/{id}?s= renders a stored setup sheet; POST /api/shop-note records a shop.note.

Auth is a per-sheet capability, s = HMAC(panel_secret, "sheet:" + sheet_id) (DESIGN.md §5.1), not the panel token.
Note rules match the shop lane's shop_note_entries (DESIGN.md §4.8), so a bad note is a 422 here.
"""

import re
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from anville.panel import make_sheet_router, sheet_sig
from tests.panel.conftest import DOC, SECRET, RecordingWriter, load

BUY = load("sheet_part_a_buy")    # one setup; the 910480 row is owned: false
FLIP = load("sheet_part_a_flip")  # two setups; every tool owned
SHEETS = {f["sheet_id"]: (f["document_id"], f["sheet"]) for f in (BUY, FLIP)}


def client(writer=None, reads=None, sheets=SHEETS):
    def read(sheet_id):
        if reads is not None:
            reads.append(sheet_id)
        return sheets.get(sheet_id)
    app = FastAPI()
    app.include_router(make_sheet_router(read, writer if writer is not None else RecordingWriter(),
                                         panel_secret=SECRET, clock=lambda: 1793300000.0))
    return TestClient(app)


def page(sheet_id, sig=None):
    return client().get(f"/sheet/{sheet_id}?s={sig or sheet_sig(SECRET, sheet_id)}")


def note(**over):
    b = {"request_id": str(uuid.uuid4()), "sheet_id": BUY["sheet_id"], "outcome": "as_planned"}
    b.update(over)
    return b


def post(c, b, sig=None):
    return c.post(f"/api/shop-note?s={sig or sheet_sig(SECRET, b.get('sheet_id', ''))}", json=b)


# --- the capability ------------------------------------------------------------------------------------------


def test_the_signature_is_per_sheet_and_per_secret():
    a = sheet_sig(SECRET, "sheet-part-a-buy")
    assert a == sheet_sig(SECRET, "sheet-part-a-buy") and re.fullmatch(r"[0-9a-f]{64}", a)
    assert a != sheet_sig(SECRET, "sheet-part-a-flip") and a != sheet_sig("other", "sheet-part-a-buy")


def test_a_bad_signature_or_unknown_sheet_is_an_empty_404_and_never_reads_the_store():
    reads = []
    c = client(reads=reads)
    good = sheet_sig(SECRET, BUY["sheet_id"])
    for url in (f"/sheet/{BUY['sheet_id']}?s=bad", f"/sheet/{BUY['sheet_id']}",
                f"/sheet/{FLIP['sheet_id']}?s={good}",  # one sheet's link never opens another
                f"/sheet/bad%20id?s={sheet_sig(SECRET, 'bad id')}"):
        r = c.get(url)
        assert (r.status_code, r.content) == (404, b""), url
    assert reads == []
    r = c.get(f"/sheet/missing?s={sheet_sig(SECRET, 'missing')}")
    assert (r.status_code, r.content) == (404, b"") and reads == ["missing"]


# --- the page ------------------------------------------------------------------------------------------------


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html))


def test_the_buy_sheet_shows_the_setup_jaws_tools_and_says_it_is_predicted():
    r = page(BUY["sheet_id"])
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    words = text(r.text)
    assert "Predicted" in words and "not yet run on the machine" in words
    assert re.search(r"Setup 1 of 1 · top up, as modelled", words)
    assert "100 × 60 × 20 mm" in words
    assert re.search(r"6 in vise, aluminum soft jaws, relieved", words)
    assert "-X jaw: pocket 13.7 × 14.7 × 22.007 mm at (-13.7, -12.207, -2.007) mm" in words
    assert re.search(r"910480 .*45° back-chamfer", words) and "to buy" in words
    assert re.search(r"EM12 .*12 mm endmill, 48 mm reach", words)
    assert "verify the back-chamfer toolpath" in words
    assert "Ran it as planned" in words and "Something was different" in words


def test_only_a_tool_the_shop_does_not_own_gets_an_arrived_checkbox():
    assert re.findall(r'data-arrived="([^"]+)"', page(BUY["sheet_id"]).text) == ["910480"]
    assert 'data-arrived=' not in page(FLIP["sheet_id"]).text
    assert "Setup 2 of 2" in text(page(FLIP["sheet_id"]).text)


def test_sheet_text_is_escaped():
    doc, sheet = SHEETS[BUY["sheet_id"]]
    evil = {**sheet, "shop": "<script>x</script>", "setups": [{**sheet["setups"][0], "notes": ['<img src=x onerror="y">']}]}
    c = client(sheets={"evil": (doc, evil)})
    html = c.get(f"/sheet/evil?s={sheet_sig(SECRET, 'evil')}").text
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert '<img src=x' not in html and "&lt;img" in html


def test_an_acquired_tool_says_where_it_came_from():
    doc, sheet = SHEETS[FLIP["sheet_id"]]
    row = {**sheet["setups"][0]["tools"][0], "acquired": {"why": "bought on 2026-10-30, from shop note abc"}}
    s = {**sheet, "setups": [{**sheet["setups"][0], "tools": [row]}]}
    html = client(sheets={"a": (doc, s)}).get(f"/sheet/a?s={sheet_sig(SECRET, 'a')}").text
    assert "bought on 2026-10-30, from shop note abc" in text(html)


# --- the shop note -------------------------------------------------------------------------------------------


def test_a_note_is_recorded_with_the_part_copied_from_the_stored_sheet():
    w = RecordingWriter()
    b = note(tools_arrived=["910480"], tags=["tool_arrived"])
    r = post(client(w), b)
    assert (r.status_code, r.json()) == (202, {"event_id": f"shop:{b['request_id']}"})
    doc, payload = w.rows[f"shop:{b['request_id']}"]
    assert doc == DOC
    assert payload == {"event": "shop.note", "note_id": b["request_id"], "sheet_id": BUY["sheet_id"],
                       "part": BUY["sheet"]["part"]["source_sha256"], "outcome": "as_planned",
                       "tools_arrived": ["910480"], "tags": ["tool_arrived"], "setups_run": None, "note_text": "",
                       "received_at": 1793300000.0}


def test_a_retried_note_returns_the_original_event_id():
    w, b = RecordingWriter(), note()
    c = client(w)
    assert post(c, b).status_code == 202
    r = post(c, b)
    assert (r.status_code, r.json()) == (200, {"event_id": f"shop:{b['request_id']}"}) and len(w.rows) == 1


def test_a_different_note_keeps_its_text_verbatim_and_null_optionals_take_defaults():
    w = RecordingWriter()
    b = note(outcome="different", note_text="  it chattered, we flipped  ", setups_run=2, tools_arrived=None, tags=None)
    assert post(client(w), b).status_code == 202
    p = w.rows[f"shop:{b['request_id']}"][1]
    assert (p["note_text"], p["setups_run"], p["tools_arrived"], p["tags"]) == ("  it chattered, we flipped  ", 2, [], [])


def test_bad_notes_are_422_and_never_written():
    w = RecordingWriter()
    c = client(w)
    for bad in (note(request_id="nope"), note(outcome="maybe"), note(outcome="different"),
                note(outcome="different", note_text="   "), note(note_text="x" * 1001),
                note(tools_arrived=["EM12"]),    # owned: nothing arrived
                note(tools_arrived=["999999"]),  # not on the sheet
                note(tools_arrived="910480"), note(tags=["coffee"]), note(tags="extra_setup"),
                note(setups_run=0), note(setups_run="2"), note(setups_run=True), note(note_text=5),
                note(extra="x"), note(part="client-lies")):  # the part comes from the stored sheet only
        assert post(c, bad).status_code == 422, bad
    assert w.rows == {}


def test_note_auth_is_an_empty_404_before_validation():
    w = RecordingWriter()
    c = client(w)
    b = note()
    for sig, body in (("bad", b), (sheet_sig(SECRET, FLIP["sheet_id"]), b),  # another sheet's capability
                      ("bad", {"sheet_id": BUY["sheet_id"], "outcome": 7})):
        r = post(c, body, sig)
        assert (r.status_code, r.content) == (404, b""), sig
    assert c.post(f"/api/shop-note?s=bad", content=b"not json").status_code == 404
    missing = note(sheet_id="missing")
    assert post(c, missing).status_code == 404  # signed, but no such sheet
    assert w.rows == {}


def test_a_setup_notes_come_before_its_workholding_because_they_say_pockets_below():
    html = page(BUY["sheet_id"]).text
    assert html.index("Cut the jaw reliefs before loading the part") < html.index("-X jaw: pocket")


def test_a_tool_named_twice_is_stored_once():
    """Codex P2: a cutter in two setups has two Arrived boxes; one tick or two is one acquisition."""
    w, b = RecordingWriter(), note(tools_arrived=["910480", "910480"])
    assert post(client(w), b).status_code == 202
    assert w.rows[f"shop:{b['request_id']}"][1]["tools_arrived"] == ["910480"]
