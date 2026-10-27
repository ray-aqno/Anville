"""Day-1 spike: Onshape webhook round trip on Modal.

    Onshape ──POST /hook/<secret>──▶ web()  ── classify (F1/F2, idempotency) ──▶ 200 immediately
                                       └── spawn ──▶ handle_event()
                                              createversion: dummy comment + STEP export, timed
                                              comment.create: log only (re-checked for self-comments)

Deploy:   modal deploy anville/modal_app.py
Register: python3 scripts/register_webhook.py <web URL printed by deploy>
"""

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
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]")
    .add_local_python_source("anville")
)
# .env is read only on the deploying machine; containers receive the values via the Secret.
if modal.is_local():
    from anville.env import load_env

    secrets = [modal.Secret.from_dict(load_env(REPO_ROOT / ".env"))]
else:
    secrets = [modal.Secret.from_dict({})]
state = modal.Dict.from_name("anville-state", create_if_missing=True)
data = modal.Volume.from_name("anville-data", create_if_missing=True)

# How long handle_event waits for the translation.complete webhook before polling.
TX_WEBHOOK_WAIT_S = 120
TX_POLL_INTERVAL_S = 15
TX_POLL_MAX = 4
# A webhook for our own comment can arrive before createComment returns (F1 race).
SELF_COMMENT_RECHECK_S = 3


class _Prefixed:
    """`x in _Prefixed("self:")` checks the shared modal.Dict without loading it."""

    def __init__(self, prefix: str):
        self.prefix = prefix

    def __contains__(self, key: object) -> bool:
        return f"{self.prefix}{key}" in state


def _log(kind: str, **fields) -> None:
    print(json.dumps({"kind": kind, "t": round(time.time(), 3), **fields}))


def _client():
    from anville.connectors.onshape.client import OnshapeClient

    return OnshapeClient.from_env(os.environ, ledger=state)


def _allowed() -> set[str]:
    from anville.env import allowed_document_ids

    return allowed_document_ids(dict(os.environ))


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
        decision = classify(event, _allowed(), _Prefixed("self:"), _Prefixed("seen:"))
        _log("webhook", event=event.get("event"), action=decision.action, reason=decision.reason,
             documentId=event.get("documentId"), versionId=event.get("versionId"))

        if decision.action == "process":
            state[f"seen:{decision.idempotency_key}"] = received_at
            if event.get("event") == TRANSLATION_COMPLETE:
                state[f"txdone:{event['translationId']}"] = received_at
            else:
                handle_event.spawn(event, received_at)
        return {"ok": True}

    return api


@app.function(image=image, secrets=secrets, volumes={"/data": data}, timeout=600)
def handle_event(event: dict, received_at: float) -> None:
    name = event.get("event")
    if name == CREATE_VERSION:
        _on_create_version(event, received_at)
    elif name == COMMENT_CREATE:
        time.sleep(SELF_COMMENT_RECHECK_S)
        if f"self:{event['commentId']}" in state:
            _log("ignored", reason="own comment (late recheck, F1)", commentId=event["commentId"])
        else:
            _log("designer_comment", commentId=event["commentId"], documentId=event["documentId"])


def _on_create_version(event: dict, received_at: float) -> None:
    client = _client()
    did, vid = event["documentId"], event["versionId"]
    timing = {"versionId": vid, "event_timestamp": event.get("timestamp"), "received_at": received_at}

    eid = _part_studio_id(client, did, vid)

    comment = client.post("/comments", {
        "documentId": did,
        "versionId": vid,
        "elementId": eid,
        "message": f"Anville received version {vid[:8]}. (Day-1 spike: webhook round trip.)",
    })
    state[f"self:{comment['id']}"] = time.time()
    timing["comment_posted_at"] = time.time()

    tx = client.post(f"/partstudios/d/{did}/v/{vid}/e/{eid}/translations",
                     {"formatName": "STEP", "storeInDocument": False, "notifyUser": False})
    tid = tx["id"]
    timing["tx_started_at"] = time.time()

    info = _wait_for_translation(client, tid, timing)
    if info.get("requestState") != "DONE":
        _log("translation_failed", tid=tid, state=info.get("requestState"), reason=info.get("failureReason"))
        state[f"timing:{vid}"] = timing
        return

    step = client.get(f"/documents/d/{did}/externaldata/{info['resultExternalDataIds'][0]}",
                      accept="application/octet-stream", raw=True)
    timing["downloaded_at"] = time.time()
    Path("/data/step").mkdir(parents=True, exist_ok=True)
    Path(f"/data/step/{vid}.step").write_bytes(step)
    data.commit()

    timing.update(
        step_bytes=len(step),
        receive_to_comment_s=round(timing["comment_posted_at"] - received_at, 2),
        export_s=round(timing["tx_done_at"] - timing["tx_started_at"], 2),
        receive_to_step_s=round(timing["downloaded_at"] - received_at, 2),
        calls_today=client.calls_today(),
    )
    state[f"timing:{vid}"] = timing
    _log("timing", **timing)


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
