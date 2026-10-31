"""The traveler (DESIGN.md §4.8, P2): the machinist's phone page for one setup sheet, and their note back.

    GET  /sheet/{sheet_id}?s=   the stored `anville.setup_sheet/1`, rendered as one HTML page
    POST /api/shop-note?s=      the machinist's note, as a `shop.note` event keyed shop:<request_id>

Auth is a per-sheet capability, s = HMAC(panel_secret, "sheet:" + sheet_id), not the panel token: the link sits
in an Onshape comment every collaborator sees, and must open that one sheet only (DESIGN.md §5.1). Main stores
the sheets and turns notes into ledger entries; this module reads and writes only through what main injects.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
import uuid
from html import escape
from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from anville.panel.router import Writer

SheetReader = Callable[[str], "tuple[str, dict] | None"]  # sheet_id -> (document_id, sheet), or None
SHEET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
TAGS = ("tool_arrived", "workholding_changed", "extra_setup", "outsourced_op")
TAG_WORDS = {"tool_arrived": "Tool arrived", "workholding_changed": "Workholding changed",
             "extra_setup": "Extra setup", "outsourced_op": "Outsourced an op"}
# The link carries the capability: keep it out of caches, referrers and search indexes.
PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}

Name = Annotated[str, StringConstraints(min_length=1, max_length=64)]


def sheet_sig(panel_secret: str, sheet_id: str) -> str:
    """The sheet's capability, for the link main posts: /sheet/<id>?s=<this>."""
    return hmac.new(panel_secret.encode(), f"sheet:{sheet_id}".encode(), hashlib.sha256).hexdigest()


class ShopNote(BaseModel):
    """The shop lane's note rules (DESIGN.md §4.8): wrong types are refused, missing or null optionals default."""
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str
    sheet_id: str
    outcome: Literal["as_planned", "different"]
    tools_arrived: list[Name] | None = Field(default=None, max_length=20)
    tags: list[Literal[TAGS]] | None = Field(default=None, max_length=len(TAGS))
    setups_run: int | None = Field(default=None, ge=1, le=99)
    note_text: str | None = Field(default=None, max_length=1000)


def _problems(note: ShopNote, sheet: dict) -> list[str]:
    """What the model alone can't check: the id is a UUID, a "different" note says what, and every tool marked
    arrived is one this sheet says the shop doesn't own yet."""
    out = []
    try:
        uuid.UUID(note.request_id)
    except ValueError:
        out.append("request_id must be a UUID")
    if note.outcome == "different" and not (note.note_text or "").strip():
        out.append("a 'different' note needs its text")
    unowned = {t.get("name") for s in sheet.get("setups", []) for t in s.get("tools", []) if t.get("owned") is False}
    out += [f"{name!r} isn't a tool this sheet is waiting for" for name in note.tools_arrived or [] if name not in unowned]
    return out


def make_sheet_router(read_sheet: SheetReader, writer: Writer, *, panel_secret: str,
                      clock: Callable[[], float] = time.time) -> APIRouter:
    """The traveler's routes. `read_sheet` and `writer` are main's store calls; both run off the event loop."""
    router = APIRouter()

    def signed(sheet_id, s) -> bool:
        # The signature is checked before any store read, so a guessed link costs nothing.
        return (isinstance(sheet_id, str) and bool(SHEET_ID.fullmatch(sheet_id)) and isinstance(s, str)
                and hmac.compare_digest(s.encode(), sheet_sig(panel_secret, sheet_id).encode()))

    @router.get("/sheet/{sheet_id}")
    async def sheet_page(sheet_id: str, s: str | None = None):
        found = await asyncio.to_thread(read_sheet, sheet_id) if signed(sheet_id, s) else None
        if found is None:
            return Response(status_code=404)
        return HTMLResponse(render_sheet(found[1], sheet_id, s), headers=PRIVATE)

    @router.post("/api/shop-note")
    async def shop_note(request: Request, s: str | None = None):
        try:
            raw = await request.json()
        except ValueError:
            raw = None
        sheet_id = raw.get("sheet_id") if isinstance(raw, dict) else None
        found = await asyncio.to_thread(read_sheet, sheet_id) if signed(sheet_id, s) else None
        if found is None:
            return Response(status_code=404)
        document_id, sheet = found
        try:
            note = ShopNote.model_validate(raw)
        except ValidationError as e:
            return JSONResponse({"detail": e.errors(include_url=False, include_context=False)}, status_code=422)
        if problems := _problems(note, sheet):
            return JSONResponse({"detail": problems}, status_code=422)
        key = f"shop:{note.request_id}"
        payload = {"event": "shop.note", "note_id": note.request_id, "sheet_id": sheet_id,
                   "part": (sheet.get("part") or {}).get("source_sha256"), "outcome": note.outcome,
                   # A cutter listed twice (two setups, two ticks) is one purchase (Codex P2).
                   "tools_arrived": list(dict.fromkeys(note.tools_arrived or [])), "tags": list(dict.fromkeys(note.tags or [])),
                   "setups_run": note.setups_run, "note_text": note.note_text or "", "received_at": clock()}
        inserted = await asyncio.to_thread(writer, key, document_id, payload)
        return JSONResponse({"event_id": key}, status_code=202 if inserted else 200)

    return router


# --- the page: plain HTML from the sheet, every value escaped --------------------------------------------------


def _n(x) -> str:
    return "—" if x is None else escape(f"{x:g}" if isinstance(x, float) else str(x))


def _mm(xs) -> str:
    return " × ".join(_n(x) for x in xs or []) + " mm"


def _tool(t: dict) -> str:
    if t.get("type") == "back_chamfer":
        what = (f"{_n(t.get('angle_deg'))}° back-chamfer, head {_n(t.get('head_diameter_mm'))} mm, "
                f"neck {_n(t.get('neck_diameter_mm'))} × {_n(t.get('neck_length_mm'))} mm")
    elif t.get("type") == "endmill":
        what = f"{_n(t.get('diameter_mm'))} mm endmill, {_n(t.get('reach_mm'))} mm reach"
    else:
        what = escape(str(t.get("type", "")))
    cuts = f"cuts {escape(', '.join(t.get('groups') or []))}" if t.get("groups") else ""
    name = escape(str(t.get("name", "")))
    buy = ""
    if t.get("owned") is False:
        buy = (f'<span class="buy">to buy</span><label class="arrived"><input type="checkbox" data-arrived="{name}"> '
               "Arrived</label>")
    why = f'<div class="why">{escape(t["acquired"]["why"])}</div>' if (t.get("acquired") or {}).get("why") else ""
    return (f'<li class="tool"><div class="row"><b>{name}</b> <span>{what}</span></div>'
            f'<div class="row small">{cuts}{buy}</div>{why}</li>')


def _workholding(w: dict) -> str:
    if w.get("type") != "vise_soft_jaws":
        return f'<p class="hold">{escape(str(w.get("status", "Workholding not stated")))}</p>'
    lines = [f"{_n(w.get('vise_in'))} in vise, {escape(str(w.get('jaw_material', '')))} soft jaws"
             + (", relieved" if w.get("relieved") else "")]
    if w.get("grip_faces"):
        lines.append(f"grip on {escape(' / '.join(w['grip_faces']))}")
    p = w.get("parallels") or {}
    if p:
        par = f"parallels {_n(p.get('height_mm'))} mm high"
        if p.get("setback_mm") is not None:
            par += f", set back {_n(p['setback_mm'])} mm from the {escape(str(p.get('from_side', '')))} side"
        lines.append(par)
    reliefs = "".join(f"<li>{escape(str(r.get('jaw', '')))}: pocket {_mm(r.get('size_mm'))} at "
                      f"({escape(', '.join(_n(v) for v in r.get('origin_mm') or []))}) mm</li>"
                      for r in w.get("reliefs") or [])
    return (f'<p class="hold">{"; ".join(lines)}</p>'
            + (f'<ul class="reliefs">{reliefs}</ul>' if reliefs else "")
            + f'<p class="status">{escape(str(w.get("status", "")))}</p>')


def _setup(s: dict, total: int) -> str:
    notes = "".join(f"<li>{escape(str(n))}</li>" for n in s.get("notes") or [])
    return (f'<section class="setup"><h2>Setup {_n(s.get("number"))} of {total} · {escape(str(s.get("orientation", "")))}'
            f' <span class="dirs">{escape(", ".join(s.get("directions") or []))}</span></h2>'
            # Notes first: the shop lane's notes point down the page ("pockets below").
            + (f'<ul class="notes">{notes}</ul>' if notes else "")
            + f'<h3>Workholding</h3>{_workholding(s.get("workholding") or {})}'
            f'<h3>Tools</h3><ul class="tools">{"".join(_tool(t) for t in s.get("tools") or [])}</ul></section>')


def render_sheet(sheet: dict, sheet_id: str, sig: str) -> str:
    setups = sheet.get("setups") or []
    part = sheet.get("part") or {}
    predicted = sheet.get("basis") == "predicted"
    unreachable = "".join(f"<li>face {_n(u.get('face_id'))}{f' ({escape(str(u.get('group')))})' if u.get('group') else ''}: "
                          f"{escape(str(u.get('reason', '')))}</li>" for u in sheet.get("unreachable") or [])
    tags = "".join(f'<label class="tag"><input type="checkbox" name="tag" value="{t}"> {TAG_WORDS[t]}</label>' for t in TAGS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setup sheet</title><link rel="stylesheet" href="/panel/assets/sheet.css"></head>
<body><main data-sheet="{escape(sheet_id)}" data-sig="{escape(sig)}">
<header><div class="brand">Anville · setup sheet</div>
{'<span class="badge">Predicted</span>' if predicted else ''}
<div class="meta">Stock {_mm(part.get("stock_mm"))} · part {escape(str(part.get("source_sha256", ""))[:8])}
{f' · {escape(str(sheet["shop"]))}' if sheet.get("shop") else ''} · {len(setups)} setup{'s' if len(setups) != 1 else ''}</div>
{''.join(f'<p class="basis">{escape(str(n))}</p>' for n in sheet.get("notes") or [])}</header>
{''.join(_setup(s, len(setups)) for s in setups)}
{f'<section class="setup"><h2>Not cut by this plan</h2><ul class="notes">{unreachable}</ul></section>' if unreachable else ''}
<form class="note" onsubmit="return false">
<div class="actions"><button type="button" class="ok" data-outcome="as_planned">✓ Ran it as planned</button>
<button type="button" class="diff" data-open>Something was different…</button></div>
<fieldset class="different" hidden><legend>What was different?</legend>
<div class="tags">{tags}</div>
<label class="runs">Setups actually run <input type="number" name="setups_run" min="1" max="99" inputmode="numeric"></label>
<textarea name="note_text" maxlength="1000" rows="4" placeholder="e.g. it chattered, so we flipped it"></textarea>
<button type="button" class="send" data-outcome="different">Send note</button></fieldset>
<p class="sent" role="status"></p></form>
</main><script type="module" src="/panel/assets/sheet.js"></script></body></html>
"""
