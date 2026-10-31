"""The fix branch (RFC 0005; ADR 0002 Amendment 2): a part edit built on its own Onshape workspace and
measured there, never on the designer's.

Under demo beat (c) the edit is the declined alternative: removing a group, which Onshape does by
suppressing the feature that created its faces. The calls (RFC 0005, "Minimum live proof"), 7-8 in all:

    1  POST workspaces from the reviewed version: anville/alt-<run>
    2  GET features: IDs, the feature's JSON, and sourceMicroversion
    3  POST featurescript: the faces each feature created, with type, area and box
    4  POST features/featureid/{fid}: the patched feature, with rejectMicroversionSkew
    5-7 STEP from the branch (storeInDocument: false), polled, downloaded

The group's faces are matched to Onshape's by area and centroid (anchor.match_centroids), and the feature is the one that
created all of them; nothing is guessed. A model writes the patched feature JSON; code checks the patch
changes only what the edit allows, and writes it itself when the model's patch fails that check. Every
number in the result is code's: `predicted` from evaluate_edits, `measured` from re-extracting the
branch's STEP. Any failure returns a `predicted` result with the reason, so the comment still says what
the edit would do.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from anville.agent.model import ChatModel, ModelUnavailable
from anville.geometry.anchor import frame_offset, match_centroids
from anville.geometry.facts import PartFacts

Emit = Callable[..., None]

# One FeatureScript call (RFC 0005): for each feature ID, the faces it created, with area and centroid in mm.
# Verified live 2026-10-31 on DEMO A (orchestrator's proof): makeId takes the REST featureId as-is, and
# `queries` must be an object. The IDs are written into the lambda, so each is checked against FEATURE_ID.
MAPPING_SCRIPT = ("function(context is Context, queries) { var out = {}; for (var fid in [%s]) { var faces = []; "
                  "for (var f in evaluateQuery(context, qCreatedBy(makeId(fid), EntityType.FACE))) { "
                  "faces = append(faces, { \"id\" : transientQueriesToStrings(f), "
                  "\"area\" : evArea(context, { \"entities\" : f }) / (millimeter * millimeter), "
                  "\"c\" : evApproximateCentroid(context, { \"entities\" : f }) / millimeter }); } "
                  "out[fid] = faces; } return out; }")
FEATURE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

PATCH_TOOL = {"type": "function", "function": {
    "name": "patched_feature",
    "description": "The whole Onshape feature JSON with the requested edit applied and nothing else changed.",
    "parameters": {"type": "object", "required": ["feature"],
                   "properties": {"feature": {"type": "object", "description": "the edited feature, in full"}}}}}
PATCH_RULES = (
    "You edit Onshape Part Studio features through the REST API. You get one feature's JSON (from GET "
    "features), the edit to make, and, for parameter edits, its feature spec. Call patched_feature with the "
    "whole feature, edited. Change only what the edit names: every other key and value stays exactly as "
    "given, in the same structure. Don't add parameters or rename anything.")
# The paths each edit may change (as JSON-path tuples). Suppression is a top-level flag (ADR 0002 Am. 1).
ALLOWED = {"suppress": {("suppressed",)}}
PATCH_MAX_TOKENS = 8192
PATCH_STAGE = "fix_patch"  # the model's trace record; the branch's own events are stage fix_branch
TX_POLL_INTERVAL_S, TX_POLL_MAX = 2.0, 30


@dataclass
class Branch:
    status: str                    # measured | predicted (nothing was measured; `reason` says why)
    group: str
    predicted: int                 # setups after the edit, from evaluate_edits
    measured: int | None = None    # setups re-counted from the branch's STEP
    name: str | None = None        # anville/alt-<run>
    url: str | None = None
    workspace_id: str | None = None
    feature_id: str | None = None
    feature_name: str | None = None
    extra_faces: int = 0           # faces the feature creates beyond the group's: suppression removes them too
    patch_by: str | None = None    # model | code
    reason: str | None = None      # why it fell back, or why code wrote the patch
    usage: list[dict] = field(default_factory=list)
    calls: int = 0                 # Onshape calls made

    def line(self) -> str:
        """The comment's line. Code writes it; every number is a field above."""
        if self.status == "measured":
            return (f"Branch {self.name}: removing {self.group} measured {self.measured} setup(s) "
                    f"(predicted {self.predicted}). Not merged: {self.url}")
        return (f"Removing {self.group} would take it to {self.predicted} setup(s): predicted from the geometry; "
                "Anville couldn't measure it on a branch this time.")

    def to_dict(self) -> dict:
        return asdict(self)


class _Counted:
    """Counts the Onshape calls this branch makes (the quota is shared; the trace says what it spent)."""

    def __init__(self, client):
        self._c, self.n = client, 0

    def get(self, path, **kw):
        self.n += 1
        return self._c.get(path, **kw)

    def post(self, path, body=None, **kw):
        self.n += 1
        return self._c.post(path, body, **kw)


class Refused(Exception):
    """A check failed: the branch is not built further, and the result says why."""


def build_branch(client, facts: PartFacts, *, did: str, vid: str, eid: str, run: str, group: str,
                 faces: list[int], predicted: int, measure: Callable[[bytes], int], emit: Emit,
                 model: ChatModel | None = None, deadline: float | None = None,
                 wait: Callable | None = None, base_url: str = "https://cad.onshape.com") -> Branch:
    """Remove `group` (its feature's `faces`, as evaluate_edits removed them) on a new branch and re-count.

    `measure(step_bytes) -> setups` re-extracts the branch's STEP with the review's shop. `wait(client, tid)`
    returns the finished translation; the default polls. Never raises: a failure is a `predicted` Branch.
    """
    api = _Counted(client)
    out = Branch(status="predicted", group=group, predicted=predicted, name=f"anville/alt-{run}")
    try:
        _build(api, facts, out, did=did, vid=vid, eid=eid, faces=faces, measure=measure, emit=emit,
               model=model, deadline=deadline, wait=wait or _poll, base_url=base_url)
    except Exception as e:  # a branch failure costs only this line, never the review
        out.reason = out.reason if isinstance(e, Refused) and out.reason else f"{type(e).__name__}: {e}"[:300]
        emit("error", "Fix branch fell back to the prediction", stage="fix_branch", reason=out.reason,
             branch=out.name)
    out.calls = api.n
    emit("verify", "Fix branch finished", stage="fix_branch", **{k: v for k, v in out.to_dict().items()
                                                                if k != "usage"}, calls_usage=out.usage)
    return out


def _build(api, facts, out: Branch, *, did, vid, eid, faces, measure, emit, model, deadline, wait, base_url):
    def refuse(why: str):
        out.reason = why
        raise Refused(why)

    ws = api.post(f"/documents/d/{did}/workspaces", {"name": out.name, "versionId": vid})
    wid = out.workspace_id = ws["id"]
    out.url = f"{base_url}/documents/{did}/w/{wid}/e/{eid}"
    emit("tool_call", "Branch created", stage="fix_branch", branch=out.name, url=out.url)
    studio = f"/partstudios/d/{did}/w/{wid}/e/{eid}"

    listing = api.get(f"{studio}/features")
    features = {f["featureId"]: f for f in listing.get("features", []) if isinstance(f, dict) and "featureId" in f}
    ids = [fid for fid in features if FEATURE_ID.match(fid)]
    if not ids:
        refuse("the feature list has no usable feature IDs")

    script = MAPPING_SCRIPT % ", ".join(json.dumps(fid) for fid in ids)
    theirs = parse_mapping(api.post(f"{studio}/featurescript", {"script": script, "queries": {}}))
    fid = feature_for(facts, faces, theirs, out)
    if fid is None:
        refuse(out.reason or "no single feature created the group's faces")
    feature = features[fid]
    out.feature_id, out.feature_name = fid, feature.get("name")
    emit("tool_result", "Feature found for the group", stage="fix_branch", group=out.group, feature_id=fid,
         feature=out.feature_name, extra_faces=out.extra_faces)

    patched = write_patch(feature, "suppress", out, emit=emit, model=model, deadline=deadline)
    res = api.post(f"{studio}/features/featureid/{fid}", {
        "btType": "BTFeatureDefinitionCall-1406", "feature": patched,
        "serializationVersion": listing.get("serializationVersion"),
        "sourceMicroversion": listing.get("sourceMicroversion"), "rejectMicroversionSkew": True})
    status = ((res or {}).get("featureState") or {}).get("featureStatus")
    if status != "OK":
        refuse(f"Onshape rebuilt the edit with status {status!r}")
    emit("tool_result", "Edit applied on the branch", stage="fix_branch", feature_id=fid, status=status)

    tx = api.post(f"{studio}/translations", {"formatName": "STEP", "storeInDocument": False, "notifyUser": False})
    info = wait(api, tx["id"])
    if info.get("requestState") != "DONE":
        refuse(f"the branch's STEP export ended {info.get('requestState')}")
    step = api.get(f"/documents/d/{did}/externaldata/{info['resultExternalDataIds'][0]}",
                   accept="application/octet-stream", raw=True)
    out.measured = int(measure(step))
    out.status = "measured"


def fs_value(v):
    """A FeatureScript result as plain Python: BTFSValueMap -> dict, Array -> list, the rest -> their value.
    Decoded by btType suffix (verified live 2026-10-31)."""
    if not isinstance(v, dict) or "btType" not in v:
        return v
    kind, value = str(v["btType"]).rsplit(".", 1)[-1], v.get("value")
    if kind == "BTFSValueMap":
        return {fs_value(e["key"]): fs_value(e["value"]) for e in value or []}
    if kind == "BTFSValueArray":
        return [fs_value(x) for x in value or []]
    return value


def parse_mapping(response) -> list[dict]:
    """The mapping script's result as faces {feature, id, area mm², c mm}. Anything else is refused."""
    result = fs_value(response.get("result")) if isinstance(response, dict) else None
    if not isinstance(result, dict):
        raise ValueError(f"the mapping script returned {type(result).__name__}, not a map")
    faces = []
    for fid, created in result.items():
        for f in created or []:
            c = f.get("c") if isinstance(f, dict) else None
            if isinstance(f.get("area"), (int, float)) and isinstance(c, list) and len(c) == 3:
                faces.append({"feature": str(fid), "id": str(f.get("id")), "area": float(f["area"]),
                              "c": [float(x) for x in c]})
    return faces


def feature_for(facts: PartFacts, faces: list[int], theirs: list[dict], out: Branch) -> str | None:
    """The one feature that created every face in `faces`, or None with `out.reason` set. All our faces are
    matched at once, so an Onshape face claimed by two of ours makes both ambiguous."""
    found = frame_offset(facts, theirs)
    if found is None:
        out.reason = "no consistent offset between our faces and Onshape's: the faces can't be matched"
        return None
    rows = match_centroids(facts, theirs, found[0])
    creator = {t["id"]: t["feature"] for t in theirs}
    missing = [f for f in faces if rows.get(f, {}).get("verdict") != "match"]
    if missing:
        out.reason = "no confident Onshape match for " + ", ".join(
            f"F{f} ({rows[f]['verdict'] if f in rows else 'not in facts'})" for f in missing)
        return None
    owners = {creator[rows[f]["id"]] for f in faces}
    if len(owners) != 1:
        out.reason = f"the group's faces come from {len(owners)} features ({', '.join(sorted(owners))})"
        return None
    fid = owners.pop()
    # Faces the feature creates that we don't remove: matched ones of ours outside `faces`.
    out.extra_faces = sum(1 for f, r in rows.items() if r["verdict"] == "match" and f not in faces
                          and creator[r["id"]] == fid)
    return fid


def changed_paths(a, b, path: tuple = ()) -> list[tuple]:
    """Where b differs from a, as JSON paths. A key added or removed is a change at that key; lists are
    compared item by item only when their lengths agree."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(a.keys() | b.keys(), key=str):
            out += [path + (k,)] if (k in a) != (k in b) else changed_paths(a[k], b[k], path + (k,))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in changed_paths(x, y, path + (i,))]
    return [] if type(a) is type(b) and a == b else [path]


def check_patch(original: dict, patched, edit: str) -> str | None:
    """Why `patched` isn't the edit, or None. Suppression must set `suppressed` true and change nothing else."""
    if not isinstance(patched, dict):
        return "the patch is not a JSON object"
    changed = set(changed_paths(original, patched))
    stray = changed - ALLOWED[edit]
    if stray:
        return "it changes more than the edit allows: " + ", ".join(".".join(map(str, p)) for p in sorted(stray, key=str)[:5])
    if edit == "suppress" and patched.get("suppressed") is not True:
        return "it doesn't set suppressed to true"
    return None


def code_patch(feature: dict, edit: str) -> dict:
    if edit == "suppress":
        return {**feature, "suppressed": True}
    raise ValueError(f"no code patch for {edit!r}")


def write_patch(feature: dict, edit: str, out: Branch, *, emit: Emit, model: ChatModel | None,
                deadline: float | None = None, specs: dict | None = None) -> dict:
    """The model's patch when it passes check_patch; otherwise code's, with the reason in the trace."""
    why = "no model configured"
    if model is not None:
        content = json.dumps({"edit": edit, "feature": feature, **({"featurespecs": specs} if specs else {})})
        messages = [{"role": "system", "content": PATCH_RULES}, {"role": "user", "content": content}]
        left = None if deadline is None else deadline - time.monotonic()
        emit("tool_call", "Model called", stage=PATCH_STAGE, model=model.name, host=getattr(model, "host", None))
        record = {"stage": PATCH_STAGE, "model": model.name, "host": getattr(model, "host", None)}
        started = time.monotonic()
        try:
            reply = model.call(messages, [PATCH_TOOL], max_tokens=PATCH_MAX_TOKENS, timeout_s=left)
        except ModelUnavailable as e:
            out.usage.append({**record, "latency_s": round(time.monotonic() - started, 3), "error": str(e)})
            why = f"model unavailable: {e}"
        else:
            out.usage.append({**record, "latency_s": round(time.monotonic() - started, 3), **(reply.usage or {})})
            if reply.error or reply.tool != "patched_feature":
                why = reply.error or f"called {reply.tool!r}, expected 'patched_feature'"
            else:
                why = check_patch(feature, reply.arguments.get("feature"), edit)
                if why is None:
                    out.patch_by = "model"
                    emit("tool_result", "Model's patch passed the check", stage=PATCH_STAGE, edit=edit)
                    return reply.arguments["feature"]
    out.patch_by = "code"
    emit("error", "Code wrote the patch instead", stage=PATCH_STAGE, reason=why)
    patched = code_patch(feature, edit)
    assert check_patch(feature, patched, edit) is None
    return patched


def _poll(api, tid: str, sleep: Callable[[float], None] = time.sleep) -> dict:
    info: dict = {}
    for _ in range(TX_POLL_MAX):
        info = api.get(f"/translations/{tid}")
        if info.get("requestState") in ("DONE", "FAILED"):
            return info
        sleep(TX_POLL_INTERVAL_S)
    return info
