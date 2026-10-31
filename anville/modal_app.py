"""Anville on Modal: webhook → durable event store → per-event jobs (ADR 0006), geometry (ADR 0008).

    Onshape ──POST /hook/<secret>──▶ web() ── classify (F1/F2) ──▶ StoreSvc.record (durable) ──▶ 200
                                                                      └── spawn ──▶ handle_event(key)
    handle_event(key): claim → STEP export → analyze() → setup count → comment → finish → spawn next
    analyze(vid):      OCP image only. STEP → PartFacts → SetupPlan
    reaper:            re-spawns events left `received`, or `claimed` past their lease
    StoreSvc:          the only process that opens the SQLite file (single writer, its own Volume)

Deploy with recreate, so two store containers never overlap (Volumes have no cross-host locking):
    modal deploy --strategy recreate anville/modal_app.py
Register: python3 scripts/register_webhook.py <web URL printed by deploy>
"""

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path

import modal

try:  # Present in the container image; module-level so FastAPI can resolve the annotation.
    from fastapi import FastAPI, HTTPException, Query, Request
except ImportError:  # local deploy environment doesn't need FastAPI
    FastAPI = HTTPException = Query = Request = None

from anville.deploy import image_ignore, panel_secret
from anville.connectors.onshape.events import (
    COMMENT_CREATE,
    CREATE_VERSION,
    TRANSLATION_COMPLETE,
    classify,
    token_matches,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# What the panel's traveler records as shop:<request_id> (ADR 0007 Amendment 3).
SHOP_NOTE_EVENT = "shop.note"

app = modal.App("anville")
_base = modal.Image.debian_slim(python_version="3.12")
# Modal ships only .py files by default: the panel's page and scripts must be let in (anville/deploy.py).
image = _base.pip_install("fastapi[standard]").add_local_python_source("anville", ignore=image_ignore)
# OCP links libGL/libX11, which its wheel doesn't bundle (ADR 0008). ~380 MB installed,
# so only analyze() uses this image; the webhook stays small and starts fast.
geo_image = (
    _base.apt_install("libgl1", "libx11-6")
    .pip_install("cadquery-ocp-novtk==8.0.1.1.0", "meta-sam-parser==0.1.0")  # SAM's mask format (ADR 0007 Am. 4)
    .add_local_python_source("anville")
)
# .env is read only on the deploying machine; containers receive the values via the Secret.
if modal.is_local():
    from anville.env import load_env

    _env = load_env(REPO_ROOT / ".env")
    # ANVILLE_* variables set when deploying override .env, so one environment can differ
    # (staging: the translation webhook goes to production's URL, so don't wait for it).
    _env.update({k: v for k, v in os.environ.items() if k.startswith("ANVILLE_")})
    secrets = [modal.Secret.from_dict(_env)]
else:
    secrets = [modal.Secret.from_dict({})]
# Ephemeral only (ADR 0006 §7): self-created IDs, translation-done flags, caches, timings.
state = modal.Dict.from_name("anville-state", create_if_missing=True)
data = modal.Volume.from_name("anville-data", create_if_missing=True)
# The database gets its own Volume, so data.commit() in jobs never touches the database file.
store_vol = modal.Volume.from_name("anville-store", create_if_missing=True)

# How long handle_event waits for the translation.complete webhook before polling.
TX_WEBHOOK_WAIT_S = float(os.environ.get("ANVILLE_TX_WAIT_S", 120))
# handle_event's Modal timeout. The agent stops calling models POST_RESERVE_S before it, so the
# version guard and the comment post (agent or setup-count fallback) always have time to run.
JOB_TIMEOUT_S = 600
POST_RESERVE_S = 60
TX_POLL_INTERVAL_S = 15
TX_POLL_MAX = 4
# A webhook for our own comment can arrive before createComment returns (F1 race).
SELF_COMMENT_RECHECK_S = 3
# The reaper only matters after a failure, so it runs rarely: each run also keeps the store warm.
REAPER_PERIOD_MIN = 5
RECEIVED_GRACE_S = 60
UNCONFIRMED_POST_S = 120

def _shop_scope() -> str:
    """The shop ledger scope for the current recording take (`modal run anville/modal_app.py::new_take`)."""
    from anville.agent.job import shop_scope

    try:
        return shop_scope(state.get("take"))
    except Exception as e:  # a state read failure falls back to the demo shop's own ledger
        _log("take_unread", error=repr(e)[:300])
        return shop_scope(None)


@app.local_entrypoint()
def new_take(name: str = "") -> None:
    """Start a recording take: from here on the shop ledger reads and writes a fresh scope, so the shop owns only
    what it owned at the start (shop lane, 2026-10-31). Nothing is deleted; `--name off` returns to the shared one."""
    take = "" if name == "off" else (name or time.strftime("take-%Y%m%d-%H%M%S"))
    state["take"] = take
    from anville.agent.job import shop_scope

    print(f"shop ledger scope: {shop_scope(take)}")


def _shop_ledger(store) -> list[dict]:
    """The shop's learned outcomes (S8): what it bought since. A ledger failure costs only the
    learning, never the review."""
    try:
        return store.call.remote("read_ledger", "shop_outcome", _shop_scope())
    except Exception as e:
        _log("shop_ledger_failed", error=repr(e)[:300])
        return []

# The Store methods a remote caller may run.
STORE_OPS = frozenset({
    "record", "claim", "finish", "reapable", "event", "is_current",
    "reserve_post", "complete_post", "already_posted", "unconfirmed_posts", "post_for_comment",
    "append_trace", "trace_after", "append_ledger", "read_ledger",
    "reserve_call", "release_call", "calls",
})
# The grant's redirect must match the OAuth app's registration exactly (ADR 0007, Amendment 1).
OAUTH_CALLBACK = "/oauth/callback"
OAUTH_STATE_TTL_S = 600


class _Prefixed:
    """`x in _Prefixed("self:")` checks the shared modal.Dict without loading it."""

    def __init__(self, prefix: str):
        self.prefix = prefix

    def __contains__(self, key: object) -> bool:
        return f"{self.prefix}{key}" in state


def _log(kind: str, **fields) -> None:
    print(json.dumps({"kind": kind, "t": round(time.time(), 3), **fields}, default=str))


class _StoreLedger:
    """OnshapeClient's CallLedger backed by the store: one atomic daily budget across containers."""

    def reserve_call(self, day: str, cap: int) -> bool:
        return StoreSvc().call.remote("reserve_call", day, cap)

    def release_call(self, day: str) -> None:
        StoreSvc().call.remote("release_call", day)

    def calls(self, day: str) -> int:
        return StoreSvc().call.remote("calls", day)


def _client():
    from anville.connectors.onshape.client import OnshapeClient

    return OnshapeClient.from_env(os.environ, ledger=_StoreLedger())


def _oauth_redirect() -> str:
    """The deployed callback URL. Built from a constant, not the request: the registered redirect must
    match exactly, and behind Modal's proxy the request may not see https."""
    return os.environ.get("ANVILLE_PUBLIC_URL", "https://ray-aqno--anville-web.modal.run") + OAUTH_CALLBACK


def _anville_client():
    """A client that acts as the Anville account (ADR 0007, Amendment 1), or None: no grant yet, or the
    refresh failed. None means the comment goes out on the owner's keys under an "Anville:" header."""
    from anville.connectors.onshape.client import OnshapeClient

    try:
        token = StoreSvc().oauth_token.remote()
    except Exception as e:
        _log("oauth_token_failed", error=repr(e)[:300])
        return None
    if not token:
        return None
    return OnshapeClient.from_env(os.environ, ledger=_StoreLedger(), auth_header=f"Bearer {token}")


def _allowed() -> set[str]:
    from anville.env import allowed_document_ids

    return allowed_document_ids(dict(os.environ))


def _event_ts(event: dict) -> float | None:
    """Onshape's webhook timestamp, e.g. '2026-10-27T01:59:05.446+00:00' (day-1 run); None if absent."""
    try:
        return dt.datetime.fromisoformat(event["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


@app.cls(image=image, secrets=secrets, volumes={"/store": store_vol}, max_containers=1, timeout=600)
@modal.concurrent(max_inputs=32)  # sync methods run on threads; Store keeps one connection per thread
class StoreSvc:
    """The only process that opens the database file (ADR 0006 §2): last write wins on a Volume."""

    @modal.enter()
    def open(self):
        from anville.store import Store

        # Store serializes after_commit itself; record() returns only once the event is durable.
        self.s = Store("/store/anville.db", after_commit=store_vol.commit)
        from anville.connectors.onshape.oauth import GrantKeeper, refresh

        # One container (max_containers=1) and one lock: each refresh rotates the refresh token.
        self.keeper = GrantKeeper(self.s, lambda g: refresh(
            g, os.environ["ONSHAPE_OAUTH_CLIENT_ID"], os.environ["ONSHAPE_OAUTH_CLIENT_SECRET"]))

    @modal.method()
    def save_grant(self, grant) -> None:
        """A new grant from the callback, saved under the keeper's lock (Codex, OAuth PR). get_grant is
        never exposed: callers get an access token from oauth_token(), never the refresh token."""
        self.keeper.replace(grant)

    @modal.method()
    def oauth_token(self) -> str | None:
        """Anville's access token, refreshed if it's near expiry; None before the account has granted access."""
        return self.keeper.token(now=time.time())

    @modal.method()
    def call(self, op: str, *args, **kw):
        return self._run(op, args, kw)

    @modal.method()
    def call_many(self, ops: list[tuple[str, tuple, dict]]) -> list:
        """Several store calls in one round trip (a job step's trace events). Not one transaction."""
        return [self._run(op, args, kw) for op, args, kw in ops]

    def _run(self, op: str, args: tuple, kw: dict):
        if op not in STORE_OPS:  # explicit: an assert would vanish under python -O
            raise ValueError(f"store op {op!r} is not allowed")
        return getattr(self.s, op)(*args, **kw)


class _Trace:
    """ADR 0007 trace events for one run, buffered and written in one store round trip per flush."""

    def __init__(self, store, document_id: str, run_id: str, version_id: str):
        self.store, self.did, self.run_id, self.vid = store, document_id, run_id, version_id
        self.element_id: str | None = None
        self.seq = 0
        self.buf: list = []

    def add(self, kind: str, title: str, **detail) -> None:
        # Titles carry no numbers; numbers go in detail (ADR 0007 contract).
        self.seq += 1
        self.buf.append(("append_trace", (self.did, self.run_id, self.seq, kind, title),
                         {"ts": time.time(), "version_id": self.vid, "element_id": self.element_id,
                          "detail": detail}))

    def flush(self) -> None:
        if self.buf:
            self.store.call_many.remote(self.buf)
            self.buf = []


@app.function(image=image, secrets=secrets, volumes={"/data": data})
@modal.asgi_app()
def web():
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # The docked panel (ADR 0007, PR #33). Without its own token it stays unmounted: a missing variable
    # must not take the webhook endpoint down with it.
    secret = panel_secret(os.environ)
    if secret:
        from anville.panel import make_router

        # No writer yet: asks get 501 until panel.ask is handled (ADR 0012's loop; Codex, PR #34).
        def read_render(version_id: str) -> bytes | None:  # SAM's picture of the flagged face (ADR 0007 Am. 4)
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", version_id or ""):
                return None
            data.reload()  # written by anchor_task's container
            path = Path(f"/data/render/{version_id}.png")
            return path.read_bytes() if path.exists() else None

        api.include_router(make_router(
            lambda d, a, n: StoreSvc().call.remote("trace_after", d, a, n), None,
            panel_secret=secret, allowed_docs=_allowed, read_render=read_render))

        # The shop's traveler (ADR 0007 Amendment 3, PR #42): the sheet the review stored, and the machinist's note.
        from anville.panel import make_sheet_router

        def read_sheet(sheet_id: str):
            ev = StoreSvc().call.remote("event", f"sheet:{sheet_id}")
            return (ev.document_id, ev.payload) if ev else None

        def write_note(key: str, document_id: str, payload: dict) -> bool:
            inserted = StoreSvc().call.remote("record", key, document_id, payload)
            if inserted:  # handled now, not at the reaper's next pass: the "arrived" tap shows up in Part B's review
                try:
                    handle_event.spawn(key)
                except Exception as e:  # the note is durable; the reaper runs it (Codex, PR #44)
                    _log("spawn_failed", key=key, error=repr(e)[:300])
            return inserted

        api.include_router(make_sheet_router(read_sheet, write_note, panel_secret=secret, read_render=read_render))
    else:
        _log("panel_unmounted", reason="ANVILLE_PANEL_TOKEN is unset or equals the webhook secret")

    @api.get("/healthz")
    async def healthz():
        import sys

        import fastapi

        return {"python": sys.version.split()[0], "fastapi": fastapi.__version__,
                "request_annotation": repr(hook.__annotations__.get("request"))}

    @api.get("/oauth/start")
    def oauth_start(key: str = ""):
        """Start the Anville account's one-time grant. Gated by its own admin key, not the webhook secret
        (Codex, OAuth PR); F2: a wrong key looks like an unknown route. Rotate the key after use: it
        travels in a query string."""
        import secrets as pysecrets

        from fastapi.responses import RedirectResponse

        from anville.connectors.onshape.oauth import admin_key_ok, authorize_url

        if not admin_key_ok(key, os.environ):
            raise HTTPException(status_code=404)
        nonce = pysecrets.token_urlsafe(24)
        state[f"oauth_state:{nonce}"] = time.time()
        return RedirectResponse(authorize_url(os.environ["ONSHAPE_OAUTH_CLIENT_ID"], _oauth_redirect(), nonce))

    @api.get(OAUTH_CALLBACK)
    def oauth_callback(code: str = "", state_: str = Query("", alias="state")):
        """Onshape sends the account back here. The state must be one /oauth/start issued, used once."""
        from dataclasses import replace
        from html import escape

        from fastapi.responses import HTMLResponse

        from anville.connectors.onshape.oauth import account_error, exchange_code

        try:
            issued = state.pop(f"oauth_state:{state_}") if state_ else None
        except KeyError:
            issued = None
        if not code or issued is None or time.time() - issued > OAUTH_STATE_TTL_S:
            raise HTTPException(status_code=404)
        grant = exchange_code(code, os.environ["ONSHAPE_OAUTH_CLIENT_ID"],
                              os.environ["ONSHAPE_OAUTH_CLIENT_SECRET"], _oauth_redirect())
        try:  # which account granted access: only the configured Anville account is saved (Codex, OAuth PR)
            from anville.connectors.onshape.client import OnshapeClient

            me = OnshapeClient.from_env(os.environ, ledger=_StoreLedger(),
                                        auth_header=f"Bearer {grant.access_token}").get("/users/sessioninfo")
        except Exception as e:
            _log("oauth_whoami_failed", error=repr(e)[:200])
            me = {}
        refused = account_error(me, os.environ.get("ANVILLE_ACCOUNT_EMAIL", ""))
        if refused:
            _log("oauth_refused", reason=refused)
            return HTMLResponse(f"<p>Not connected: {escape(refused)}.</p>", status_code=403)
        grant = replace(grant, user=str(me.get("name") or ""))
        StoreSvc().save_grant.remote(grant)
        _log("oauth_granted", user=grant.user)
        return HTMLResponse(f"<p>Anville is connected as <b>{escape(grant.user or 'this account')}</b>. "
                            "Comments will post under its name.</p>")

    @api.post("/hook/{token}")
    async def hook(token: str, request: Request):
        # F2: unknown token looks exactly like an unknown route.
        if not token_matches(token, os.environ["ANVILLE_WEBHOOK_SECRET"]):
            raise HTTPException(status_code=404)
        received_at = time.time()
        event = await request.json()
        # No seen-set here: the store's record() is the de-duplication authority (ADR 0006).
        decision = classify(event, _allowed(), _Prefixed("self:"), ())
        _log("webhook", event=event.get("event"), action=decision.action, reason=decision.reason,
             documentId=event.get("documentId"), versionId=event.get("versionId"))
        if decision.action != "process":
            return {"ok": True}

        if event.get("event") == TRANSLATION_COMPLETE:
            state[f"txdone:{event['translationId']}"] = received_at  # idempotent; ephemeral (§7)
            return {"ok": True}

        key = decision.idempotency_key
        version_id = event.get("versionId") if event.get("event") == CREATE_VERSION else None
        try:
            inserted = await StoreSvc().call.remote.aio(
                "record", key, event["documentId"], event,
                version_id=version_id, version_ts=_event_ts(event))
        except Exception as e:
            # ADR 0006 §3: non-200 only when the insert itself fails.
            _log("record_failed", key=key, error=repr(e)[:300])
            raise HTTPException(status_code=500) from None
        _log("recorded", key=key, inserted=inserted, record_s=round(time.time() - received_at, 3))
        if inserted:
            try:
                await handle_event.spawn.aio(key)
            except Exception as e:
                # The event is durable, so this is still a 200 (ADR 0006 §3); the reaper
                # picks it up after RECEIVED_GRACE_S.
                _log("spawn_failed", key=key, error=repr(e)[:300])
        return {"ok": True}

    return api


@app.function(image=image, secrets=secrets, volumes={"/data": data}, timeout=JOB_TIMEOUT_S)
def handle_event(key: str) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_S - POST_RESERVE_S
    from anville.store import StaleClaim

    store = StoreSvc()
    token = store.call.remote("claim", key)
    if token is None:
        # Done already, claimed by a live job, out of attempts, or another event on its document is running.
        _log("not_claimed", key=key)
        return
    ev = store.call.remote("event", key)
    ok = False
    try:
        name = ev.payload.get("event")
        if name == CREATE_VERSION:
            _on_create_version(store, ev, deadline)
        elif name == SHOP_NOTE_EVENT:
            _on_shop_note(store, ev)
        elif name == COMMENT_CREATE:
            time.sleep(SELF_COMMENT_RECHECK_S)
            cid = ev.payload["commentId"]
            if f"self:{cid}" in state:
                _log("ignored", reason="own comment (late recheck, F1)", commentId=cid)
            else:
                _log("designer_comment", commentId=cid, documentId=ev.document_id)
                _on_reply(store, ev, deadline)
        ok = True
    finally:
        # A failed job is marked failed, not retried: the reaper only retries lost or expired claims.
        try:
            nxt = store.call.remote("finish", key, token, ok)
        except StaleClaim:
            _log("stale_claim", key=key, token=token)  # our lease expired and the event was re-claimed
            nxt = None
        if nxt:
            handle_event.spawn(nxt)


def _on_shop_note(store, ev) -> None:
    """A machinist's note from the traveler (ADR 0007 Amendment 3) goes to the shop ledger, so the next review
    starts from it: a cutter ticked as arrived becomes one the shop owns (demo gaps 2 and 3)."""
    from anville.agent.job import shop_note_writes

    note = ev.payload
    sheet = store.call.remote("event", f"sheet:{note.get('sheet_id')}")
    if sheet is None:
        _log("ignored", reason="shop note for an unknown sheet", key=ev.key, sheet_id=note.get("sheet_id"))
        return
    try:
        entries = shop_note_writes(note, sheet.payload, stored=store.call.remote("read_ledger", "shop_outcome",
                                                                                 _shop_scope()),
                                   sheet_document=sheet.document_id, note_document=ev.document_id)
    except ValueError as e:  # the panel validates first; a note that still fails costs only itself
        _log("shop_note_rejected", key=ev.key, error=str(e)[:300])
        return
    for entry in entries:
        store.call.remote("append_ledger", "shop_outcome", _shop_scope(), entry["payload"], entry["provenance"])
    _log("shop_note_recorded", key=ev.key, kinds=[e["payload"]["kind"] for e in entries])


def _store_sheet(store, key: str, did: str, vid: str, facts: dict, back_chamfer: list[dict], ledger: list[dict],
                 buying: list[str], trace: "_Trace") -> str | None:
    """The setup sheet the shop runs, stored for the traveler as event sheet:<id>. A sheet failure costs only the
    sheet, never the review."""
    from anville.agent.job import review_shop, sheet_for, sheet_id_for

    try:
        shop, learned = review_shop(ledger)
        sheet = sheet_for(facts, shop, back_chamfer, buying=buying, provenance=learned)
        sid = sheet_id_for(key)  # a retry keeps the first sheet: record ignores the duplicate key
        store.call.remote("record", f"sheet:{sid}", did, {**sheet, "version_id": vid}, settled=True)
    except Exception as e:
        _log("sheet_failed", error=repr(e)[:300])
        return None
    trace.add("tool_result", "Setup sheet ready", sheet_id=sid, setups=len(sheet["setups"]), buying=buying)
    return sid


def _on_create_version(store, ev, deadline: float) -> None:
    event = ev.payload
    did, vid = event["documentId"], event["versionId"]
    received_at = ev.received_at
    timing = {"versionId": vid, "event_timestamp": event.get("timestamp"), "received_at": received_at,
              "attempt": ev.attempts}
    trace = _Trace(store, did, f"{vid[:8]}-{ev.attempts}", vid)
    trace.add("run_started", "Reviewing a new version")
    try:
        _review(store, ev, trace, timing, deadline)
    except Exception as e:
        # Close the trace so the panel shows a failed run, not one stuck mid-step.
        trace.add("error", "Review failed", error=type(e).__name__, message=str(e)[:300])
        trace.add("run_finished", "Review ended with an error")
        trace.flush()
        raise


def _on_reply(store, ev, deadline: float) -> None:
    """ADR 0012: a reply under an Anville comment becomes constraints, and the same version is re-planned and
    answered in the thread. Any other comment stays quiet."""
    from anville.agent.reply import is_own_comment, resolve_review

    cid, did = ev.payload["commentId"], ev.document_id
    client = _client()
    reply = client.get(f"/comments/{cid}")
    posts = lambda c: store.call.remote("post_for_comment", c)  # noqa: E731
    if is_own_comment(reply, posts):  # durable, not just state: the self-loop guard (Codex, PR #38)
        _log("ignored", reason="own comment (durable check, F1)", commentId=cid)
        return
    root = reply.get("parentId")  # inference until the live check (ADR 0012, open question 1)
    review_key = resolve_review(root, posts, lambda c: client.get(f"/comments/{c}").get("parentId")) if root else None
    if review_key is None:
        _log("ignored", reason="not a reply to Anville", commentId=cid)
        return
    review = store.call.remote("event", review_key)
    if review is None or "versionId" not in review.payload:
        _log("ignored", reason="reply thread has no review on record", commentId=cid, parentId=root)
        return
    vid = review.payload["versionId"]
    trace = _Trace(store, did, f"{vid[:8]}-r{ev.attempts}", vid)
    trace.add("run_started", "Reading a designer's reply", comment_id=cid)
    try:
        _replan(store, ev, trace, client, reply, root, review, vid, deadline)
    except Exception as e:
        trace.add("error", "Re-plan failed", error=type(e).__name__, message=str(e)[:300])
        trace.add("run_finished", "Re-plan ended with an error")
        trace.flush()
        raise


def _replan(store, ev, trace: "_Trace", client, reply: dict, root: str, review, vid: str,
            deadline: float) -> None:
    from anville.agent.job import agent_comment, flushing_at_stage_changes, read_reply_turn, review_shop, with_credits
    from anville.agent.reply import constraint_entry, constraints_from_ledger, nothing_read, unrecorded

    cid, did, review_key = ev.payload["commentId"], ev.document_id, review.key
    ledger = _shop_ledger(store)
    result = analyze.remote(vid, ledger)  # the version's STEP is already on the volume: no second export
    shop, learned = review_shop(ledger)
    # The issues the designer is answering are the review's: the shop as it was then (Codex, PR #38).
    then, _ = review_shop([r for r in ledger if r.get("created_at", 0) <= review.received_at])
    emit = flushing_at_stage_changes(trace.add, trace.flush)
    found, groups, setups, (read_calls, read_fallbacks) = read_reply_turn(result["facts"], shop, os.environ, emit,
                                                                          reply.get("message") or "",
                                                                          deadline=deadline,
                                                                          back_chamfer=result["back_chamfer"],
                                                                          issue_shop=then)
    scope = f"doc:{did}"
    entries = [constraint_entry(rule, reply_id=cid, review_key=review_key, version_id=vid) for rule in found or []]
    for entry in unrecorded(entries, store.call.remote("read_ledger", "constraint", scope)):  # a retry adds nothing
        store.call.remote("append_ledger", "constraint", scope, entry["payload"], entry["provenance"])
    for rule in found or []:
        trace.add("tool_result", "Constraint recorded", face_group=rule["face_group"], rule=rule["rule"],
                  quote=rule["quote"])
    if found:
        constraints = constraints_from_ledger(store.call.remote("read_ledger", "constraint", scope), version_id=vid)
        comment, stats = agent_comment(result["facts"], shop, os.environ, emit, deadline=deadline,
                                       back_chamfer=result["back_chamfer"], learned=learned, constraints=constraints)
        # The reader worked even when the review falls back to the setup count (Codex, P2): credit what ran.
        message = with_credits(comment or _review_message(vid, result["plan"]),
                               read_calls + stats.get("agent_usage", []),
                               [*read_fallbacks, *stats.get("agent_fallbacks", [])])
    else:
        message = with_credits(nothing_read(groups, setups), read_calls, read_fallbacks) if groups else None
    trace.flush()
    if message and store.call.remote("reserve_post", ev.key, "replan"):
        from anville.connectors.onshape.oauth import post_as_anville

        posted, identity = post_as_anville(_anville_client(), client, {
            "documentId": did, "parentId": root, "message": message})
        state[f"self:{posted['id']}"] = time.time()
        store.call.remote("complete_post", ev.key, "replan", posted["id"])
        trace.add("comment_posted", "Answered in the thread", comment_id=posted["id"], posted_as=identity)
    trace.add("run_finished", "Re-plan finished")
    trace.flush()


def _review(store, ev, trace: "_Trace", timing: dict, deadline: float) -> None:
    event = ev.payload
    did, vid = event["documentId"], event["versionId"]
    received_at = ev.received_at
    client = _client()

    eid = _part_studio_id(client, did, vid)
    trace.element_id = eid

    tx = client.post(f"/partstudios/d/{did}/v/{vid}/e/{eid}/translations",
                     {"formatName": "STEP", "storeInDocument": False, "notifyUser": False})
    tid = tx["id"]
    timing["tx_started_at"] = time.time()
    trace.add("tool_call", "Exporting STEP", translation_id=tid)
    trace.flush()

    info = _wait_for_translation(client, tid, timing)
    if info.get("requestState") != "DONE":
        _log("translation_failed", tid=tid, state=info.get("requestState"), reason=info.get("failureReason"))
        state[f"timing:{vid}"] = timing
        raise RuntimeError(f"STEP export {tid} ended {info.get('requestState')}")

    step = client.get(f"/documents/d/{did}/externaldata/{info['resultExternalDataIds'][0]}",
                      accept="application/octet-stream", raw=True)
    timing["downloaded_at"] = time.time()
    Path("/data/step").mkdir(parents=True, exist_ok=True)
    Path(f"/data/step/{vid}.step").write_bytes(step)
    data.commit()

    ledger = _shop_ledger(store)
    result = analyze.remote(vid, ledger)
    timing["analyzed_at"] = time.time()
    plan = result["plan"]
    trace.add("verify", "Setup count computed from geometry", setups=plan["count"],
              directions=[s["directions"] for s in plan["setups"]],
              unreachable=len(plan["unreachable"]), faces=result["faces"], extract_s=result["extract_s"])
    comment, calls, fallbacks = _agent_comment(result["facts"], result["back_chamfer"], ledger, trace, timing,
                                               deadline)
    message = comment or _review_message(vid, plan)
    timing["sheet_id"] = _store_sheet(store, ev.key, did, vid, result["facts"], result["back_chamfer"], ledger,
                                      timing.get("buying") or [], trace)
    from anville.deploy import sheet_link

    link = sheet_link(os.environ, timing["sheet_id"])
    if link:  # the setup sheet goes to the shop with the review (demo spine, beat 2)
        message += f"\nSetup sheet for the shop: {link}"
    if comment:  # the models that wrote this review, named under it (rubric: visible Meta tooling)
        from anville.agent.job import with_credits

        message = with_credits(message, calls, fallbacks)
    timing["reviewed_at"] = time.time()

    # Version guard (ADR 0006 §5): a superseded version gets no comment.
    if not store.call.remote("is_current", did, vid):
        trace.add("run_finished", "Superseded by a newer version; nothing posted")
        trace.flush()
        _log("superseded", versionId=vid)
        return

    # Reserve before posting, so a crash mid-post can't produce a second comment.
    if store.call.remote("reserve_post", ev.key, "review"):
        from anville.connectors.onshape.oauth import post_as_anville

        comment, identity = post_as_anville(_anville_client(), client, {
            "documentId": did,
            "versionId": vid,
            "elementId": eid,
            "message": message,
        })
        state[f"self:{comment['id']}"] = time.time()
        store.call.remote("complete_post", ev.key, "review", comment["id"])
        timing["comment_posted_at"] = time.time()
        trace.add("comment_posted", "Posted the review", comment_id=comment["id"], posted_as=identity)
        try:  # SAM outlines the flagged face for the panel and the sheet, after the comment is up (ADR 0007 Am. 4)
            from anville.agent.job import flagged_groups, review_shop

            groups = flagged_groups(result["facts"], review_shop(ledger)[0], result["back_chamfer"])
            if groups:
                anchor_task.spawn(did, vid, eid, groups[0])
        except Exception as e:  # a picture must never cost the review
            _log("anchor_spawn_failed", error=repr(e)[:300])
        if os.environ.get("ANVILLE_FIX_BRANCH", "off") == "on":
            # A branch export takes ~51 s (RFC 0005, live), so the branch runs on its own once the review is up
            # and answers in its thread. It holds no document claim: a designer's reply isn't kept waiting.
            fix_branch_task.spawn(ev.key, did, vid, eid, comment["id"])
    trace.add("run_finished", "Review finished")
    trace.flush()

    timing.update(
        step_bytes=len(step),
        export_s=round(timing["tx_done_at"] - timing["tx_started_at"], 2),
        receive_to_step_s=round(timing["downloaded_at"] - received_at, 2),
        analyze_s=round(timing["analyzed_at"] - timing["downloaded_at"], 2),
        review_s=round(timing["reviewed_at"] - timing["analyzed_at"], 2),
        calls_today=client.calls_today(),
    )
    if "comment_posted_at" in timing:
        timing["receive_to_comment_s"] = round(timing["comment_posted_at"] - received_at, 2)
    state[f"timing:{vid}"] = timing
    _log("timing", **timing)


@app.function(image=geo_image, secrets=secrets, volumes={"/data": data}, timeout=120)
def anchor_task(did: str, vid: str, eid: str, group: str) -> None:
    """SAM 3.1 outlines the face the review flagged; the PNG goes to the panel and the setup sheet (ADR 0007
    Amendment 4). Code picks and renders the group; SAM confirms the outline or is shown to disagree."""
    from anville.geometry import PartFacts
    from anville.geometry.sam_anchor import SamClient, anchor

    data.reload()
    store = StoreSvc()
    trace = _Trace(store, did, f"{vid[:8]}-a", vid)
    trace.element_id = eid
    trace.add("run_started", "Pointing at the flagged face")
    sam = SamClient.from_env(os.environ)
    if sam is not None:
        trace.add("tool_call", "Model called", stage="anchor", model=sam.model, host=sam.host)
    trace.flush()
    facts = PartFacts.from_dict(json.loads(Path(f"/data/facts/{vid}.json").read_text()))
    a = anchor(f"/data/step/{vid}.step", facts, group, sam=sam)
    if a is None:
        trace.add("run_finished", "The flagged face isn't visible from any view")
    else:
        Path("/data/render").mkdir(parents=True, exist_ok=True)
        Path(f"/data/render/{vid}.png").write_bytes(a.png)
        data.commit()
        trace.add("tool_result", "Face anchored", **a.record, version_id=vid)
        trace.add("run_finished", "Face anchored")
    trace.flush()
    _log("anchor", **(a.record if a else {"group": group, "visible": False}))


@app.function(image=image, secrets=secrets, volumes={"/data": data}, timeout=300)
def fix_branch_task(key: str, did: str, vid: str, eid: str, root: str) -> None:
    """Demo beat (c): the removal built on an anville/alt branch, measured, and answered under the review
    (RFC 0005; ADR 0002 Amendment 2). Once per review: a second spawn for the same review does nothing."""
    from anville.agent.job import fix_branch, flushing_at_stage_changes, review_shop, with_credits

    if state.get(f"branch:{key}"):
        return
    state[f"branch:{key}"] = time.time()
    store = StoreSvc()
    trace = _Trace(store, did, f"{vid[:8]}-b", vid)
    trace.element_id = eid
    trace.add("run_started", "Building the alternative on a branch")
    ledger = _shop_ledger(store)
    result = analyze.remote(vid, ledger)  # the review's STEP is on the volume: no second export
    shop, _ = review_shop(ledger)
    run = hashlib.sha256(key.encode()).hexdigest()[:6]

    def measure(step: bytes) -> int:  # the branch's STEP, counted by the same geometry and shop as the review
        alt = f"{vid}-alt-{run}"
        Path(f"/data/step/{alt}.step").write_bytes(step)
        data.commit()
        return analyze.remote(alt, ledger)["plan"]["count"]

    client = _client()
    line, stats = fix_branch(client, result["facts"], shop, os.environ,
                             flushing_at_stage_changes(trace.add, trace.flush), did=did, vid=vid, eid=eid, run=run,
                             measure=measure, back_chamfer=result["back_chamfer"],
                             wait=lambda c, tid: _wait_for_translation(c, tid, {}))
    branch = stats.get("fix_branch") if isinstance(stats.get("fix_branch"), dict) else {}
    if line and store.call.remote("reserve_post", key, "branch"):
        from anville.connectors.onshape.oauth import post_as_anville

        posted, identity = post_as_anville(_anville_client(), client, {
            "documentId": did, "parentId": root, "message": with_credits(line, branch.get("usage", []))})
        state[f"self:{posted['id']}"] = time.time()
        store.call.remote("complete_post", key, "branch", posted["id"])
        trace.add("comment_posted", "Answered in the thread", comment_id=posted["id"], posted_as=identity)
    trace.add("run_finished", "Branch finished")
    trace.flush()
    _log("fix_branch", **{k: v for k, v in branch.items() if k != "usage"}, calls_today=client.calls_today())


def _agent_comment(facts: dict, back_chamfer: list[dict], ledger: list[dict], trace: "_Trace", timing: dict,
                   deadline: float) -> tuple[str | None, list[dict], list[str]]:
    """The agent's review (ADR 0009), or None so the job posts the plain setup-count line; with its call records
    and fallbacks, for the credits line."""
    from anville.agent.job import agent_comment, flushing_at_stage_changes, review_shop

    shop, learned = review_shop(ledger)
    if learned:  # the panel lights "learned": this review starts from what the shop told us
        trace.add("tool_result", "Used what the shop learned", tools=learned)
    # Each stage's events reach the store when the next stage starts: the panel animates live.
    comment, stats = agent_comment(facts, shop, os.environ, flushing_at_stage_changes(trace.add, trace.flush),
                                   deadline=deadline, back_chamfer=back_chamfer, learned=learned)
    trace.flush()
    usage = stats.pop("agent_usage", [])
    timing.update(stats, agent_tokens=sum(u.get("completion_tokens") or 0 for u in usage))
    if "agent_error" in stats:
        _log("agent_failed", error=stats["agent_error"])
    return comment, usage, stats.get("agent_fallbacks", [])


def _review_message(vid: str, plan: dict) -> str:
    """Every number here comes from the SetupPlan (locked decision 3)."""
    dirs = "; ".join(" + ".join(s["directions"]) for s in plan["setups"]) or "none"
    msg = f"Anville reviewed version {vid[:8]}: {plan['count']} setup(s) on a 3-axis mill ({dirs})."
    # The two reasons have different fixes: a tool the shop could buy vs. a design change.
    no_tool = sum(u["reason"] == "no_tool" for u in plan["unreachable"])
    no_dir = sum(u["reason"] == "no_direction" for u in plan["unreachable"])
    if no_tool:
        msg += f" {no_tool} face(s) need a tool the shop doesn't have."
    if no_dir:
        msg += f" {no_dir} face(s) can't be reached from any of the six directions."
    return msg + " Geometry only so far; costed options come next."


@app.function(image=geo_image, volumes={"/data": data}, timeout=300)
def analyze(version_id: str, ledger: list[dict] = ()) -> dict:
    """STEP → PartFacts → SetupPlan (ADR 0008). PartFacts are kept on the Volume for later stages."""
    from dataclasses import replace

    from anville.agent.job import catalog_for, review_shop
    from anville.geometry import assess_back_chamfer, extract, min_setups

    data.reload()  # the STEP was committed by handle_event's container
    step = f"/data/step/{version_id}.step"
    facts = extract(step)
    shop, _ = review_shop(list(ledger))  # the same shop the review prices: owned cutters included (S8)
    try:
        # Checks a-f for the shop's cutters and the catalog's (RFC 0002), on this part. The review gives
        # the same list to the plan, the options and the ask gate; a catalog cutter is a purchase to offer.
        access = assess_back_chamfer(step, facts, replace(
            shop, back_chamfer_tools=[*shop.back_chamfer_tools, *catalog_for(shop)]))
    except Exception as e:  # a back-chamfer bug costs only the buy option, never the review
        _log("back_chamfer_failed", error=repr(e)[:300])
        access = []
    plan = min_setups(facts, shop, back_chamfer=access)
    Path("/data/facts").mkdir(parents=True, exist_ok=True)
    Path(f"/data/facts/{version_id}.json").write_text(json.dumps(facts.to_dict()))
    data.commit()
    return {"plan": plan.to_dict(), "facts": facts.to_dict(), "back_chamfer": [a.to_dict() for a in access],
            "faces": len(facts.faces),
            "groups": len(facts.groups), "extract_s": round(facts.extract_s, 2)}


@app.function(image=image, schedule=modal.Period(minutes=REAPER_PERIOD_MIN), timeout=120)
def reaper() -> None:
    """ADR 0006 §4: re-spawn events whose spawn was lost or whose job died past its lease."""
    store = StoreSvc()
    for ev in store.call.remote("reapable", RECEIVED_GRACE_S):
        _log("reaped", key=ev.key, status=ev.status, attempts=ev.attempts)
        handle_event.spawn(ev.key)
    stuck = store.call.remote("unconfirmed_posts", UNCONFIRMED_POST_S)
    if stuck:
        _log("unconfirmed_posts", posts=stuck)  # flagged for a human, never reposted


def _part_studio_id(client, did: str, vid: str) -> str:
    # Keyed by version: versions are immutable, but a later one may have replaced the studio.
    key = f"elem:{did}:{vid}"
    cached = state.get(key)
    if cached:
        return cached
    elements = client.get(f"/documents/d/{did}/v/{vid}/elements?elementType=PARTSTUDIO")
    eid = elements[0]["id"]
    state[key] = eid
    return eid


def _wait_for_translation(client, tid: str, timing: dict) -> dict:
    # Prefer the free translation.complete webhook; poll (counted calls) only as a fallback.
    deadline = time.time() + TX_WEBHOOK_WAIT_S
    while time.time() < deadline:
        done_at = state.get(f"txdone:{tid}")
        if done_at:
            timing["tx_done_at"], timing["tx_done_via"] = done_at, "webhook"
            return client.get(f"/translations/{tid}")
        time.sleep(1)

    info: dict = {}
    for _ in range(TX_POLL_MAX):
        info = client.get(f"/translations/{tid}")
        if info.get("requestState") in ("DONE", "FAILED"):
            timing["tx_done_at"], timing["tx_done_via"] = time.time(), "poll"
            return info
        time.sleep(TX_POLL_INTERVAL_S)
    timing["tx_done_via"] = "timeout"
    return info
