"""The fix branch (RFC 0005; ADR 0002 Amendment 2) against a fake Onshape: no live calls.

The fake answers in the shapes the orchestrator's live proof recorded on DEMO A (2026-10-31), kept in
data/onshape_fix_branch_live.json, so the decoding is tested on what Onshape really returned.
"""

import copy
import json
from pathlib import Path

import pytest

from anville.agent.job import fix_branch, removal_alternative
from anville.agent.model import ModelUnavailable, Reply
from anville.connectors.onshape.fixbranch import (
    MAPPING_SCRIPT, build_branch, changed_paths, check_patch, fs_value, parse_mapping,
)
from anville.geometry import min_setups

LIVE = json.loads((Path(__file__).parent / "data" / "onshape_fix_branch_live.json").read_text())
CHAMFER_FEATURE = LIVE["weld_chamfer_feature"]


def _fs(v):
    """Encode plain Python as FeatureScript values, the way the live result came back."""
    t = "com.belmonttech.serialize.fsvalue."
    if isinstance(v, dict):
        return {"btType": t + "BTFSValueMap", "typeTag": "", "value": [
            {"btType": "BTFSValueMapEntry-2077", "key": _fs(k), "value": _fs(x)} for k, x in v.items()]}
    if isinstance(v, list):
        return {"btType": t + "BTFSValueArray", "typeTag": "", "value": [_fs(x) for x in v]}
    if isinstance(v, str):
        return {"btType": t + "BTFSValueString", "typeTag": "", "value": v}
    return {"btType": t + "BTFSValueNumber", "typeTag": "", "value": v}


class FakeOnshape:
    """Answers the branch's 7-8 calls. `owners`: our face id -> the feature that created it."""

    def __init__(self, facts, owners: dict[int, str], *, shift=(0.0, 0.0, 0.0), status="OK",
                 tx_states=("ACTIVE", "DONE"), duplicate_of: int | None = None):
        self.calls, self.facts, self.owners, self.shift = [], facts, owners, shift
        self.status, self.tx_states, self.duplicate_of = status, list(tx_states), duplicate_of
        self.features = [{**copy.deepcopy(CHAMFER_FEATURE), "featureId": fid, "name": f"Feature {fid}"}
                         for fid in sorted(set(owners.values()))]

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        if path.endswith("/features"):
            return {"features": self.features, "serializationVersion": "1.2.23", "sourceMicroversion": "mv1"}
        if path.startswith("/translations/"):
            state = self.tx_states.pop(0)
            return {"requestState": state, "resultExternalDataIds": ["x1"] if state == "DONE" else []}
        if "/externaldata/" in path:
            assert kw == {"accept": "application/octet-stream", "raw": True}
            return b"ISO-10303-21; branch"
        raise AssertionError(path)

    def post(self, path, body=None, **kw):
        self.calls.append(("POST", path, body))
        if path.endswith("/workspaces"):
            return {"id": "w-alt"}
        if path.endswith("/featurescript"):
            out: dict = {}
            for f in self.facts.faces:
                c = [a + s for a, s in zip(f.centroid, self.shift)]
                out.setdefault(self.owners[f.id], []).append({"id": f"T{f.id}", "area": f.area, "c": c})
                if f.id == self.duplicate_of:  # a second face, same area and place: no match is confident
                    out[self.owners[f.id]].append({"id": f"T{f.id}b", "area": f.area, "c": c})
            return {"result": _fs(out), "console": "", "notices": []}
        if "/features/featureid/" in path:
            return {"featureState": {"featureStatus": self.status, "inactive": False}, "feature": body["feature"]}
        if path.endswith("/translations"):
            return {"id": "tx1"}
        raise AssertionError(path)


class FakeWonder:
    name, host = "muse-wonder-1.0", "api.meta.ai"

    def __init__(self, patch=None, error=None):
        self.patch, self.error, self.seen = patch, error, []

    def call(self, messages, tools, **kw):
        self.seen.append(json.loads(messages[1]["content"]))
        if self.error:
            raise ModelUnavailable(self.error)
        feature = self.patch(copy.deepcopy(self.seen[-1]["feature"]))
        return Reply(tool="patched_feature", arguments={"feature": feature}, finish_reason="tool_calls",
                     usage={"prompt_tokens": 600, "completion_tokens": 168})


def _suppress(f):
    f["suppressed"] = True
    return f


@pytest.fixture(scope="module")
def removal(part_a, shop):
    return removal_alternative(part_a.to_dict(), shop)


@pytest.fixture
def owners(part_a, removal):
    return {f.id: ("Fchamfer" if f.id in removal.removed_faces else "Fblock") for f in part_a.faces}


def _build(part_a, removal, api, *, model=None, events=None, measure=lambda step: 1):
    log = events if events is not None else []
    return build_branch(api, part_a, did="d1", vid="v1", eid="e1", run="r7", group=removal.face_group,
                        faces=removal.removed_faces, predicted=removal.after.setups, measure=measure,
                        emit=lambda kind, title, **d: log.append((kind, title, d)), model=model,
                        wait=lambda client, tid: _wait(client, tid))


def _wait(client, tid):
    while True:
        info = client.get(f"/translations/{tid}")
        if info["requestState"] in ("DONE", "FAILED"):
            return info


def test_the_removal_is_part_as_bottom_chamfer_2_to_1(part_a, shop, removal, chamfer):
    assert removal.face_group == chamfer
    assert (removal.before.setups, removal.after.setups) == (2, 1)


def test_the_branch_suppresses_the_chamfer_and_measures_it(part_a, removal, owners):
    api = FakeOnshape(part_a, owners)
    seen = []
    b = _build(part_a, removal, api, measure=lambda step: seen.append(step) or 1)
    assert (b.status, b.predicted, b.measured, b.feature_id, b.patch_by) == ("measured", 1, 1, "Fchamfer", "code")
    assert seen == [b"ISO-10303-21; branch"]
    assert b.name == "anville/alt-r7" and b.url == "https://cad.onshape.com/documents/d1/w/w-alt/e/e1"
    assert b.calls == 8 and len(api.calls) == 8  # RFC 0005: 7-8 calls (two polls here)
    assert api.calls[0] == ("POST", "/documents/d/d1/workspaces", {"name": "anville/alt-r7", "versionId": "v1"})
    assert all("/w/w-alt/" in p for m, p, _ in api.calls[1:5])  # every edit is on the branch, never main
    assert b.line() == (f"Branch anville/alt-r7: removing {removal.face_group} measured 1 setup(s) (predicted 1). "
                        "Not merged: https://cad.onshape.com/documents/d1/w/w-alt/e/e1")


def test_the_requests_have_the_live_proofs_shapes(part_a, removal, owners):
    api = FakeOnshape(part_a, owners)
    _build(part_a, removal, api)
    (fs,) = [b for m, p, b in api.calls if p.endswith("/featurescript")]
    assert fs["queries"] == {}  # a list gets HTTP 400 (live)
    assert '["Fblock", "Fchamfer"]' in fs["script"]
    (update,) = [b for m, p, b in api.calls if p.endswith("/features/featureid/Fchamfer")]
    assert update["btType"] == "BTFeatureDefinitionCall-1406"
    assert (update["serializationVersion"], update["sourceMicroversion"], update["rejectMicroversionSkew"]) == \
        ("1.2.23", "mv1", True)
    assert changed_paths(api.features[1], update["feature"]) == [("suppressed",)]
    (tx,) = [b for m, p, b in api.calls if p.endswith("/translations")]
    assert tx == {"formatName": "STEP", "storeInDocument": False, "notifyUser": False}


def test_a_part_off_the_origin_still_matches(part_a, removal, owners):
    """DEMO A is modelled centred on the origin; the frame offset is found, not assumed."""
    b = _build(part_a, removal, FakeOnshape(part_a, owners, shift=(50.0, 30.0, -10.0)))
    assert (b.status, b.feature_id) == ("measured", "Fchamfer")


def test_muse_wonders_patch_is_used_when_it_only_suppresses(part_a, removal, owners):
    api, model, events = FakeOnshape(part_a, owners), FakeWonder(_suppress), []
    b = _build(part_a, removal, api, model=model, events=events)
    assert b.patch_by == "model" and b.status == "measured"
    assert model.seen[0]["edit"] == "suppress" and model.seen[0]["feature"]["featureId"] == "Fchamfer"
    (rec,) = b.usage
    assert (rec["stage"], rec["model"], rec["host"], rec["completion_tokens"]) == \
        ("fix_patch", "muse-wonder-1.0", "api.meta.ai", 168)
    assert ("tool_call", "Model called") in [(k, t) for k, t, _ in events]


@pytest.mark.parametrize("bad", [
    lambda f: {**_suppress(f), "name": "Renamed"},                                        # a second change
    lambda f: (_suppress(f), f["parameters"][3].update(expression="3 mm"))[0],            # a parameter too
    lambda f: {**f, "suppressed": "true"},                                                # wrong type
    lambda f: {k: v for k, v in _suppress(f).items() if k != "parameters"},              # drops a key
], ids=["renames", "changes width", "string true", "drops parameters"])
def test_a_patch_that_does_more_falls_back_to_codes(part_a, removal, owners, bad):
    api = FakeOnshape(part_a, owners)
    b = _build(part_a, removal, api, model=FakeWonder(bad))
    assert b.patch_by == "code" and b.status == "measured"
    (update,) = [b for m, p, b in api.calls if "/features/featureid/" in p]
    assert changed_paths(api.features[1], update["feature"]) == [("suppressed",)]


def test_an_unreachable_model_falls_back_to_codes_patch(part_a, removal, owners):
    events = []
    b = _build(part_a, removal, FakeOnshape(part_a, owners), model=FakeWonder(error="HTTP 503"), events=events)
    assert b.patch_by == "code" and b.status == "measured" and "error" in b.usage[0]
    assert any(t == "Code wrote the patch instead" and "503" in d["reason"] for _, t, d in events)


def test_an_ambiguous_face_is_never_guessed(part_a, removal, owners):
    api = FakeOnshape(part_a, owners, duplicate_of=removal.removed_faces[0])
    b = _build(part_a, removal, api)
    assert b.status == "predicted" and "ambiguous" in b.reason and b.measured is None
    assert not [p for m, p, _ in api.calls if "/features/featureid/" in p]  # nothing was edited
    assert b.line() == (f"Removing {removal.face_group} would take it to 1 setup(s): predicted from the geometry; "
                        "Anville couldn't measure it on a branch this time.")


def test_a_group_from_two_features_is_refused(part_a, removal, owners):
    """Suppressing one feature wouldn't remove the group: a block face joins the chamfer's."""
    faces = removal.removed_faces + [next(f.id for f in part_a.faces if f.id not in removal.removed_faces)]
    api = FakeOnshape(part_a, owners)
    b = build_branch(api, part_a, did="d1", vid="v1", eid="e1", run="r7", group="G1", faces=faces, predicted=1,
                     measure=lambda s: 1, emit=lambda *a, **k: None, wait=_wait)
    assert b.status == "predicted" and "2 features" in b.reason


def test_a_failed_rebuild_stops_before_the_export(part_a, removal, owners):
    api = FakeOnshape(part_a, owners, status="ERROR")
    b = _build(part_a, removal, api)
    assert b.status == "predicted" and "'ERROR'" in b.reason
    assert not [p for m, p, _ in api.calls if p.endswith("/translations")]


def test_a_failed_export_is_predicted_not_measured(part_a, removal, owners):
    b = _build(part_a, removal, FakeOnshape(part_a, owners, tx_states=("FAILED",)))
    assert b.status == "predicted" and "FAILED" in b.reason


def test_an_unsafe_feature_id_never_reaches_the_script(part_a, removal, owners):
    api = FakeOnshape(part_a, owners)
    api.features.append({**CHAMFER_FEATURE, "featureId": 'x"]); evil(['})
    _build(part_a, removal, api)
    (fs,) = [b for m, p, b in api.calls if p.endswith("/featurescript")]
    assert "evil" not in fs["script"] and fs["script"].startswith(MAPPING_SCRIPT.split("%s")[0])


def test_the_live_result_decodes_to_demo_as_features():
    """Real response, DEMO A: the weld chamfer created 1 face of 212.1 mm²; the Block 8, the Pocket 9."""
    faces = parse_mapping({"result": LIVE["featurescript_result"]})
    sizes = sorted(len([f for f in faces if f["feature"] == fid]) for fid in {f["feature"] for f in faces})
    assert sizes == [1, 1, 8, 9]
    assert any(abs(f["area"] - 212.1) < 0.05 for f in faces)
    assert all(len(f["c"]) == 3 for f in faces)


def test_fs_value_reads_maps_arrays_numbers_strings():
    assert fs_value(_fs({"a": [1.5, "x"], "b": {"c": 2}})) == {"a": [1.5, "x"], "b": {"c": 2}}


def test_check_patch_only_allows_suppressed():
    f = {"suppressed": False, "parameters": [{"value": 0.0}]}
    assert check_patch(f, {"suppressed": True, "parameters": [{"value": 0.0}]}, "suppress") is None
    assert "parameters.0.value" in check_patch(f, {"suppressed": True, "parameters": [{"value": 1.0}]}, "suppress")
    assert "parameters" in check_patch(f, {"suppressed": True, "parameters": []}, "suppress")
    assert "true" in check_patch(f, f, "suppress")
    assert check_patch(f, None, "suppress") == "the patch is not a JSON object"


def test_the_job_skips_the_branch_unless_switched_on(part_a, shop):
    class NoCalls:
        def get(self, *a, **k):
            raise AssertionError("no Onshape call when the branch is off")
        post = get

    line, stats = fix_branch(NoCalls(), part_a.to_dict(), shop, {}, lambda *a, **k: None, did="d", vid="v",
                             eid="e", run="r", measure=lambda s: 1)
    assert (line, stats) == (None, {"fix_branch": "off"})


def test_the_job_builds_the_branch_and_returns_its_line(part_a, shop, removal, owners):
    api, events = FakeOnshape(part_a, owners), []
    line, stats = fix_branch(api, part_a.to_dict(), shop, {"ANVILLE_FIX_BRANCH": "on"},
                             lambda kind, title, **d: events.append((kind, title, d)), did="d1", vid="v1",
                             eid="e1", run="r7", measure=lambda s: 1, wait=_wait)
    assert line.startswith(f"Branch anville/alt-r7: removing {removal.face_group} measured 1 setup(s) (predicted 1)")
    assert stats["fix_branch"]["status"] == "measured" and stats["fix_branch"]["calls"] == 8
    assert any(t == "Fix branch finished" and d["stage"] == "fix_branch" for _, t, d in events)


def test_the_job_has_nothing_to_build_on_a_one_setup_part(part_a, shop, removal):
    from anville.geometry.edits import _without

    one = _without(part_a, set(removal.removed_faces))
    assert min_setups(one, shop).count == 1
    line, stats = fix_branch(None, one.to_dict(), shop, {"ANVILLE_FIX_BRANCH": "on"}, lambda *a, **k: None,
                             did="d", vid="v", eid="e", run="r", measure=lambda s: 1)
    assert (line, stats) == (None, {"fix_branch": "nothing to build"})
