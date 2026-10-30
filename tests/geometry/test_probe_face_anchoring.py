"""scripts/probe_face_anchoring.py runs once against a rate-limited API: these pin down the Codex review's findings."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "probe_face_anchoring", Path(__file__).parents[2] / "scripts" / "probe_face_anchoring.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# Ours: (face_id, surface, area mm², box lo mm, box hi mm). Two mirror-image planes and a hole.
OURS = [(1, "plane", 100.0, (0, 0, 0), (10, 10, 0)),
        (2, "plane", 100.0, (100, 0, 0), (110, 10, 0)),
        (3, "cylinder", 50.0, (40, 0, 0), (45, 5, 10))]


def _theirs(box=lambda lo, hi: {"minCorner": [c / 1000 for c in lo], "maxCorner": [c / 1000 for c in hi]}):
    return [{"id": f"J{fid}", "area": area / 1e6, "surface": {"type": kind.upper()}, "box": box(lo, hi)}
            for fid, kind, area, lo, hi in OURS]


def _ids(report):
    return {r["face"]: r["onshape_id"] for r in report["rows"] if r["verdict"] == "match"}


def test_boxes_pair_every_face():
    report = probe.pair(OURS, _theirs())
    assert report["scale_to_mm"] == 1000 and _ids(report) == {1: "J1", 2: "J2", 3: "J3"} and report["matched"] == 3


def test_no_box_is_never_a_match():
    """A lone type-and-area candidate can sit anywhere: the 100 mm² plane at x=100 is not proof for x=0."""
    theirs = [f for f in _theirs() if f["id"] != "J2"]
    for f in theirs:
        del f["box"]
    report = probe.pair(OURS, theirs)
    assert report["matched"] == 0 and _ids(report) == {}


def test_other_bodies_do_not_skew_the_units():
    """A second body in the Part Studio adds area that our one-solid STEP doesn't have."""
    other = {"id": "K1", "area": 0.01, "surface": {"type": "PLANE"},
             "box": {"minCorner": [1, 1, 1], "maxCorner": [1.1, 1.1, 1]}}
    report = probe.pair(OURS, [*_theirs(), other])
    assert report["scale_to_mm"] == 1000 and report["matched"] == 3


def test_corners_given_as_xyz_dicts_parse():
    report = probe.pair(OURS, _theirs(box=lambda lo, hi: {
        "minCorner": dict(zip("xyz", (c / 1000 for c in lo))), "maxCorner": dict(zip("xyz", (c / 1000 for c in hi)))}))
    assert report["matched"] == 3


def test_an_unreadable_box_does_not_crash():
    report = probe.pair(OURS, _theirs(box=lambda lo, hi: {"corners": "?"}))
    assert report["matched"] == 0


def test_two_faces_claiming_one_id_are_both_ambiguous():
    twins = [(1, "plane", 100.0, (0, 0, 0), (10, 10, 0)), (2, "plane", 100.0, (0.01, 0, 0), (10.01, 10, 0))]
    theirs = [{"id": "J1", "area": 100.0 / 1e6, "surface": {"type": "PLANE"},
               "box": {"minCorner": [0.000005, 0, 0], "maxCorner": [0.010005, 0.01, 0]}}]
    report = probe.pair(twins, theirs)
    assert report["matched"] == 0 and all(r["verdict"] == "ambiguous" and r["onshape_id"] is None
                                          for r in report["rows"])


def test_unknown_surface_names_are_reported_not_guessed():
    theirs = _theirs()
    theirs[0]["surface"] = {"type": "PLANE_SURFACE"}
    theirs[1]["surface"] = None
    report = probe.pair(OURS, theirs)
    assert report["unknown_surface_types"] == ["None", "PLANE_SURFACE"] and _ids(report) == {3: "J3"}


def test_bad_arguments_stop_before_the_call(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(probe, "_client", lambda: called.append(1))
    with pytest.raises(SystemExit):
        probe.main(["--step", str(tmp_path / "v.step"), "--vid", "V", "--eid", "E"])  # no --did
    with pytest.raises(SystemExit):
        probe.main(["--step", str(tmp_path / "missing.step"), "--did", "D", "--vid", "V", "--eid", "E"])
    assert called == []
