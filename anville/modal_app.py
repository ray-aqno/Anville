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
import json
import os
import time
from pathlib import Path

import modal

try:  # Present in the container image; module-level so FastAPI can resolve the annotation.
    from fastapi import FastAPI, HTTPException, Request
except ImportError:  # local deploy environment doesn't need FastAPI
    FastAPI = HTTPException = Request = None

from anville.connectors.onshape.events import (
    COMMENT_CREATE,
    CREATE_VERSION,
    TRANSLATION_COMPLETE,
    classify,
    token_matches,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

app = modal.App("anville")
_base = modal.Image.debian_slim(python_version="3.12")
image = _base.pip_install("fastapi[standard]").add_local_python_source("anville")
# OCP links libGL/libX11, which its wheel doesn't bundle (ADR 0008). ~380 MB installed,
# so only analyze() uses this image; the webhook stays small and starts fast.
geo_image = (
    _base.apt_install("libgl1", "libx11-6")
    .pip_install("cadquery-ocp-novtk==8.0.1.1.0")
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

# Placeholder shop until the shop-profile format is decided (CLAUDE.md §4); same tools as the geometry tests.
DEFAULT_TOOLS = (("EM12", 12.0, 4.0), ("EM6", 6.0, 4.0), ("EM3", 3.0, 4.0))


def _shop():
    """The shop the plan is made for: end mills it owns, no back-chamfer cutter yet, relieved soft
    jaws (RFC 0002 §3). The catalog's cutters are offered as purchases, never counted as owned."""
    from anville.geometry import ShopProfile, SoftJaws, Tool

    return ShopProfile(tools=[Tool(*t) for t in DEFAULT_TOOLS], fixture=SoftJaws())

# The Store methods a remote caller may run.
STORE_OPS = frozenset({
    "record", "claim", "finish", "reapable", "event", "is_current",
    "reserve_post", "complete_post", "already_posted", "unconfirmed_posts",
    "append_trace", "trace_after", "append_ledger", "read_ledger",
    "reserve_call", "release_call", "calls",
})


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


def _allowed() -> set[str]:
    from anville.env import allowed_document_ids

    return allowed_document_ids(dict(os.environ))


def _event_ts(event: dict) -> float | None:
    """Onshape's webhook timestamp, e.g. '2026-10-27T01:59:05.446+00:00' (day-1 run); None if absent."""
    try:
        return dt.datetime.fromisoformat(event["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


@app.cls(image=image, volumes={"/store": store_vol}, max_containers=1, timeout=600)
@modal.concurrent(max_inputs=32)  # sync methods run on threads; Store keeps one connection per thread
class StoreSvc:
    """The only process that opens the database file (ADR 0006 §2): last write wins on a Volume."""

    @modal.enter()
    def open(self):
        from anville.store import Store

        # Store serializes after_commit itself; record() returns only once the event is durable.
        self.s = Store("/store/anville.db", after_commit=store_vol.commit)

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


@app.function(image=image, secrets=secrets)
@modal.asgi_app()
def web():
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/healthz")
    async def healthz():
        import sys

        import fastapi

        return {"python": sys.version.split()[0], "fastapi": fastapi.__version__,
                "request_annotation": repr(hook.__annotations__.get("request"))}

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
        elif name == COMMENT_CREATE:
            time.sleep(SELF_COMMENT_RECHECK_S)
            cid = ev.payload["commentId"]
            if f"self:{cid}" in state:
                _log("ignored", reason="own comment (late recheck, F1)", commentId=cid)
            else:
                _log("designer_comment", commentId=cid, documentId=ev.document_id)
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

    result = analyze.remote(vid)
    timing["analyzed_at"] = time.time()
    plan = result["plan"]
    trace.add("verify", "Setup count computed from geometry", setups=plan["count"],
              directions=[s["directions"] for s in plan["setups"]],
              unreachable=len(plan["unreachable"]), faces=result["faces"], extract_s=result["extract_s"])
    message = _agent_comment(result["facts"], result["back_chamfer"], trace, timing, deadline) or _review_message(vid, plan)
    timing["reviewed_at"] = time.time()

    # Version guard (ADR 0006 §5): a superseded version gets no comment.
    if not store.call.remote("is_current", did, vid):
        trace.add("run_finished", "Superseded by a newer version; nothing posted")
        trace.flush()
        _log("superseded", versionId=vid)
        return

    # Reserve before posting, so a crash mid-post can't produce a second comment.
    if store.call.remote("reserve_post", ev.key, "review"):
        comment = client.post("/comments", {
            "documentId": did,
            "versionId": vid,
            "elementId": eid,
            "message": message,
        })
        state[f"self:{comment['id']}"] = time.time()
        store.call.remote("complete_post", ev.key, "review", comment["id"])
        timing["comment_posted_at"] = time.time()
        trace.add("comment_posted", "Posted the review", comment_id=comment["id"])
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


def _agent_comment(facts: dict, back_chamfer: list[dict], trace: "_Trace", timing: dict,
                   deadline: float) -> str | None:
    """The agent's review (ADR 0009), or None so the job posts the plain setup-count line."""
    from anville.agent.job import agent_comment

    comment, stats = agent_comment(facts, _shop(), os.environ, trace.add, deadline=deadline,
                                   back_chamfer=back_chamfer)
    trace.flush()
    usage = stats.pop("agent_usage", [])
    timing.update(stats, agent_tokens=sum(u.get("completion_tokens") or 0 for u in usage))
    if "agent_error" in stats:
        _log("agent_failed", error=stats["agent_error"])
    return comment


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
def analyze(version_id: str) -> dict:
    """STEP → PartFacts → SetupPlan (ADR 0008). PartFacts are kept on the Volume for later stages."""
    from dataclasses import replace

    from anville.geometry import BACK_CHAMFER_CATALOG, assess_back_chamfer, extract, min_setups

    data.reload()  # the STEP was committed by handle_event's container
    step = f"/data/step/{version_id}.step"
    facts = extract(step)
    shop = _shop()
    try:
        # Checks a-f for the shop's cutters and the catalog's (RFC 0002), on this part. The review gives
        # the same list to the plan, the options and the ask gate; a catalog cutter is a purchase to offer.
        access = assess_back_chamfer(step, facts, replace(
            shop, back_chamfer_tools=[*shop.back_chamfer_tools, *BACK_CHAMFER_CATALOG]))
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
