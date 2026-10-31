"""The panel's HTTP surface (ADR 0007 Contract + Amendment 2): the page, its assets, the run trace, the ask.

The panel only reads trace events and writes `panel.ask` events, through the `reader` and `writer`
that main injects (the store service). It never opens the store, and never calls Onshape.

    GET  /panel?documentId=&t=              the page Onshape loads in its right panel
    GET  /panel/assets/{name}               static code (an allowlist; no data, no token)
    GET  /api/runs?documentId=&t=           the latest runs with their events, for first paint
    GET  /api/stream?documentId=&after=&t=  server-sent trace events with cursor > after
    POST /api/ask?t=                        a designer's selection + question, as a panel.ask event
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Annotated, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from anville.connectors.onshape.events import token_matches

Reader = Callable[[str, int, int], list[dict]]  # trace_after(document_id, after, limit), oldest first
Writer = Callable[..., bool]                     # record(key, document_id, payload): False on a duplicate key

STATIC = Path(__file__).parent / "static"
ASSETS = frozenset({"panel.js", "panel.css", "reduce.js", "pace.js", "view.js"})
MEDIA = {".js": "text/javascript", ".css": "text/css"}
RUNS_KEPT = 5
PAGE = 500
MAX_PAGES = 2000  # 1M events at PAGE 500: a runaway guard; the stream resumes from the cursor reached

Id = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class Ask(BaseModel):
    """The panel's ask (ADR 0007): a client UUID, reused on retries, and the faces the designer pointed at."""
    model_config = ConfigDict(extra="forbid")
    request_id: str
    documentId: Id
    workspaceId: Id
    elementId: Id
    microversion_id: Id | None = None
    face_ids: list[Id] = Field(max_length=20)
    question: str = Field(max_length=500)

    @field_validator("request_id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        uuid.UUID(v)  # ValueError -> 422
        return v


def make_router(reader: Reader, writer: Writer | None, *, panel_secret: str, allowed_docs: Callable[[], set[str]],
                poll_s: float = 1.0, heartbeat_s: float = 15.0, stream_s: float = 55.0,
                clock: Callable[[], float] = time.time) -> APIRouter:
    """The panel's routes. `allowed_docs` is called per request, so the allowlist can change without a restart.
    `writer=None` refuses asks with 501: until a panel.ask handler exists, a 202 would promise an answer that never
    comes (Codex, PR #34)."""
    router = APIRouter()

    def allowed(t: str | None, document_id: str | None) -> bool:
        # Same check as the webhook secret (F2): constant time, and every refusal is an empty 404.
        return bool(t) and token_matches(t, panel_secret) and bool(document_id) and document_id in allowed_docs()

    @router.get("/panel")
    async def panel(documentId: str | None = None, t: str | None = None):
        if not allowed(t, documentId):
            return Response(status_code=404)
        return FileResponse(STATIC / "panel.html", media_type="text/html")

    @router.get("/panel/assets/{name}")
    async def asset(name: str):
        if name not in ASSETS:
            return Response(status_code=404)
        return FileResponse(STATIC / name, media_type=MEDIA[Path(name).suffix])

    @router.get("/api/runs")
    async def runs(documentId: str | None = None, t: str | None = None):
        if not allowed(t, documentId):
            return Response(status_code=404)
        kept, cursor = await asyncio.to_thread(_latest_runs, reader, documentId)
        return {"runs": [{"run_id": rid, "events": evs} for rid, evs in kept.items()], "cursor": cursor}

    @router.get("/api/stream")
    async def stream(request: Request, documentId: str | None = None, after: int = 0, t: str | None = None):
        if not allowed(t, documentId):
            return Response(status_code=404)
        # EventSource reconnects with its original URL, so Last-Event-ID is the real resume point.
        try:
            after = max(after, int(request.headers.get("last-event-id", "")))
        except ValueError:
            pass
        return StreamingResponse(_frames(reader, documentId, after, poll_s, heartbeat_s, stream_s, clock),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/api/ask")
    async def ask(request: Request, t: str | None = None):
        # Auth before validation, so a wrong token never learns the route exists from a 422.
        try:
            raw = await request.json()
        except ValueError:
            raw = None
        doc = raw.get("documentId") if isinstance(raw, dict) else None
        if not allowed(t, doc if isinstance(doc, str) else None):
            return Response(status_code=404)
        try:
            a = Ask.model_validate(raw)
        except ValidationError as e:
            return JSONResponse({"detail": e.errors(include_url=False, include_context=False)}, status_code=422)
        if writer is None:
            return JSONResponse({"detail": "asks aren't answered yet"}, status_code=501)
        key = f"ask:{a.request_id}"
        payload = {**a.model_dump(), "event": "panel.ask", "received_at": clock()}
        inserted = await asyncio.to_thread(writer, key, a.documentId, payload)
        return JSONResponse({"event_id": key}, status_code=202 if inserted else 200)

    return router


async def _frames(reader: Reader, document_id: str, after: int, poll_s: float, heartbeat_s: float,
                  stream_s: float, clock: Callable[[], float]):
    """SSE frames for events with cursor > after, until stream_s passes; the browser then reconnects."""
    start = last_sent = clock()
    yield "retry: 1000\n\n"
    while clock() - start < stream_s:
        for e in await asyncio.to_thread(reader, document_id, after, PAGE):
            after = e["cursor"]
            last_sent = clock()
            yield f"id: {after}\nevent: trace\ndata: {json.dumps(e, separators=(',', ':'))}\n\n"
        if clock() - last_sent >= heartbeat_s:
            last_sent = clock()
            yield ": hb\n\n"
        await asyncio.sleep(poll_s)


def _latest_runs(reader: Reader, document_id: str) -> tuple[dict[str, list[dict]], int]:
    """The latest RUNS_KEPT runs, oldest first, and the last cursor read.

    The store reads forward only, so this pages to the end, keeping only the latest runs in memory.
    A trace_tail store op would avoid the paging on long documents (ADR 0007 Amendment 2 §3).
    """
    kept: dict[str, list[dict]] = {}
    after = 0
    for _ in range(MAX_PAGES):
        page = reader(document_id, after, PAGE)
        for e in page:
            if e["run_id"] not in kept and len(kept) == RUNS_KEPT:
                del kept[next(iter(kept))]  # drop the oldest run
            kept.setdefault(e["run_id"], []).append(e)
        if page:
            after = page[-1]["cursor"]
        if len(page) < PAGE:
            break
    return kept, after
