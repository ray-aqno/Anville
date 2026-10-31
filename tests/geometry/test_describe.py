"""describe_group: every group on the test parts gets the right noun phrase, or None. Never a wrong one.

The expected strings were checked against each part's builder (tests/geometry/parts) and DEMO A's build
sheet: the pocket sizes, hole diameters, chamfer legs and sides are the ones the parts were modelled with.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from anville.geometry import PartFacts, describe_group, extract

DEMO_A = {
    "G1": "the left face", "G2": "the front face",
    "G3": "the 1.5 mm chamfer on the bottom front edge",
    "G4": "the top face", "G5": "the bottom face",
    "G6": "the 1 mm chamfer on the top back edge",
    "G7": "the back face", "G8": "the right face",
    "G9": "the left Ø8.5 through hole",
    "G10": "the walls of the 8 mm deep 50 × 30 mm pocket (r6 corners)",
    "G11": "the right Ø8.5 through hole",
    "G12": "the floor of the 8 mm deep 50 × 30 mm pocket (r6 corners)",
}

# Only the groups a rule describes; every other group on these parts must be None.
DESCRIBED = {
    "demo_bracket": DEMO_A,
    "demo_bracket_b": {
        "G3": "the 1.5 mm chamfer on the bottom left edge",
        "G8": "the left Ø10.5 through hole",
        "G9": "the walls of the 8 mm deep 40 × 20 mm pocket (r5 corners)",
        "G10": "the floor of the 8 mm deep 40 × 20 mm pocket (r5 corners)",
    },
    "side_hole": {
        "G7": "the walls of the 8 mm deep 40 × 25 mm pocket (r5 corners)",
        "G8": "the walls of the 6 mm deep 30 × 20 mm pocket on the bottom (r5 corners)",
        "G9": "the 15 mm deep Ø8 hole in the right face",
        "G10": "the floor of the 8 mm deep 40 × 25 mm pocket (r5 corners)",
        "G11": "the floor of the 6 mm deep 30 × 20 mm pocket on the bottom (r5 corners)",
        "G12": "the bottom of the 15 mm deep Ø8 hole in the right face",
    },
    "deep_slot": {"G7": "the walls of the 45 mm deep, 3 mm wide slot"},
    "stopped_chamfer": {"G3": "the 1.5 mm chamfer on the bottom front edge"},
    "both_long_chamfers": {"G3": "the 1.5 mm chamfer on the bottom back edge",
                           "G4": "the 1.5 mm chamfer on the bottom front edge"},
    # Steps, an open slot, a wedge's sloped face and an internal bore fit no rule exactly: None, not a guess.
    "thin_wall_step": {}, "clevis": {}, "wedge": {}, "undercut": {
        "G7": "the walls of the 15 mm deep 40 × 25 mm pocket (r5 corners)",
        "G8": "the floor of the 15 mm deep 40 × 25 mm pocket (r5 corners)",
    },
}


def _stock_face(phrase: str | None) -> bool:
    return phrase in {f"the {s} face" for s in ("top", "bottom", "front", "back", "left", "right")}


@pytest.mark.parametrize("name", sorted(DESCRIBED))
def test_each_group_reads_as_built_or_none(name, facts):
    f = facts(name)
    for label in f.labelled_groups():
        got = describe_group(f, label)
        if label in DESCRIBED[name]:
            assert got == DESCRIBED[name][label], label
        elif name != "demo_bracket":  # stock faces aren't listed on the other parts
            assert got is None or _stock_face(got), (label, got)


def test_demo_a_v1_reads_the_same_although_centred_on_the_origin():
    """DEMO A v1, the STEP production reviewed: Rayyan modelled it centred (x −50..50, y −30..30), so the
    sides come from the part's bbox. G11 is the weld chamfer, on the bottom front edge."""
    f = extract(Path(__file__).parent / "data" / "onshape_demo_a_v1.step")
    assert f.bbox[0][0] < -49 and f.bbox[0][1] < -29
    got = {g: describe_group(f, g) for g in f.labelled_groups()}
    assert got["G11"] == "the 1.5 mm chamfer on the bottom front edge"
    assert sorted(got.values()) == sorted(DEMO_A.values())  # same part, its own group order


def test_the_onshape_z_bracket_gets_no_wrong_pocket(facts):
    """Its stepped legs once read as "pocket floors"; they have a convex edge where they run out."""
    f = extract(Path(__file__).parent / "data" / "onshape_z_bracket.step")
    for label in f.labelled_groups():
        got = describe_group(f, label)
        assert got is None or _stock_face(got), (label, got)


def test_facts_without_face_boxes_describe_only_stock_faces(facts):
    """PartFacts stored before faces carried a box: no measurement is invented."""
    f = facts("demo_bracket")
    old = PartFacts.from_dict({**f.to_dict(), "faces": [{**x, "box": None} for x in f.to_dict()["faces"]]})
    assert describe_group(old, "G4") == "the top face"
    assert [describe_group(old, g) for g in ("G3", "G9", "G10", "G12")] == [None] * 4


def test_an_unknown_label_is_an_error(facts):
    with pytest.raises(ValueError, match="unknown face group"):
        describe_group(facts("demo_bracket"), "G99")


def test_a_hole_in_a_pocket_floor_is_none_and_the_floor_still_reads(tmp_path):
    """A Ø6 hole, 5 mm deep, from two_pockets' top pocket floor: no rule describes a hole that starts inside a
    pocket yet, so it and its bottom are None (not "a 6 mm wide slot"); the floor around it is still the floor."""
    from tests.geometry.parts import _cut, _cylinder, two_pockets, write_step

    f = extract(write_step(_cut(two_pockets(), _cylinder((40, 25, 12), (0, 0, 1), 3, 6)), tmp_path / "ph.step"))
    got = {g: describe_group(f, g) for g in f.labelled_groups()}
    hole = [g for g in got if all(x.surface == "cylinder" and x.radius == pytest.approx(3) for x in f.faces
                                  if x.id in f.group_faces(g))]
    bottom = [g for g in got if any(x.normal and x.normal[2] > 0.99 and x.area == pytest.approx(28.2743, rel=1e-3)
                                    for x in f.faces if x.id in f.group_faces(g))]
    assert len(hole) == 1 and len(bottom) == 1
    assert got[hole[0]] is None and got[bottom[0]] is None
    assert "the floor of the 8 mm deep 40 × 25 mm pocket (r5 corners)" in got.values()


def test_a_pocket_open_to_the_front_is_not_called_a_pocket(tmp_path):
    """Cut through the front face, its walls and floor run out of the part: a step, so no pocket is claimed."""
    from tests.geometry.parts import _cut, _pocket_body, plain_block, write_step

    f = extract(write_step(_cut(plain_block(), _pocket_body(20, -10, 17, 40, 35, 10, 5)), tmp_path / "open.step"))
    got = [describe_group(f, g) for g in f.labelled_groups()]
    assert not [p for p in got if p and "pocket" in p], got


# --- Codex review: geometry that once got a confident, wrong phrase ---


def _phrases(shape, tmp_path, name):
    from tests.geometry.parts import write_step

    f = extract(write_step(shape, tmp_path / f"{name}.step"))
    return f, {g: describe_group(f, g) for g in f.labelled_groups()}


def _group_of(f, test):
    return next(g for g, ids in f.labelled_groups().items() if any(test(x) for x in f.faces if x.id in ids))


def test_a_half_round_channel_on_an_edge_is_not_a_through_hole(tmp_path):
    from tests.geometry.parts import _cut, _cylinder, plain_block

    _, got = _phrases(_cut(plain_block(), _cylinder((0, 25, -1), (0, 0, 1), 4, 27)), tmp_path, "channel")
    assert not [p for p in got.values() if p and "hole" in p], got


def test_a_counterbore_is_not_a_blind_hole_and_its_shoulder_no_hole_bottom(tmp_path):
    """Ø6 through with a Ø10 × 5 counterbore from the top: no rule covers it yet, so no hole is named."""
    from tests.geometry.parts import _cut, _cylinder, plain_block

    part = _cut(_cut(plain_block(), _cylinder((40, 25, -1), (0, 0, 1), 3, 27)), _cylinder((40, 25, 20), (0, 0, 1), 5, 6))
    _, got = _phrases(part, tmp_path, "cbore")
    assert not [p for p in got.values() if p and "Ø" in p], got


def test_a_shelf_open_at_the_front_is_not_a_pocket_floor(tmp_path):
    """A stepped pocket: its upper level drops away at the front (a convex edge on its outline)."""
    from tests.geometry.parts import _box, _cut, plain_block

    part = _cut(_cut(plain_block(), _box(20, 12.5, 15, 40, 25, 15)), _box(20, 12.5, 8, 40, 12, 20))
    f, got = _phrases(part, tmp_path, "shelf")
    shelf = _group_of(f, lambda x: x.normal and x.normal[2] > 0.99 and x.box and abs(x.box[0][2] - 15) < 1e-6)
    assert got[shelf] is None


def test_a_pocket_in_a_pocket_floor_gives_one_depth_for_its_walls_and_floor(tmp_path):
    from tests.geometry.parts import _cut, _pocket_body, plain_block

    part = _cut(_cut(plain_block(), _pocket_body(20, 12.5, 17, 40, 25, 10, 5)), _pocket_body(30, 17.5, 12, 20, 15, 10, 3))
    _, got = _phrases(part, tmp_path, "nested")
    assert "the floor of the 5 mm deep 20 × 15 mm pocket (r3 corners)" in got.values(), got
    assert "the walls of the 5 mm deep 20 × 15 mm pocket (r3 corners)" in got.values(), got
    assert "the floor of the 8 mm deep 40 × 25 mm pocket (r5 corners)" in got.values(), got


def test_a_rotated_pocket_reports_no_bounding_box_as_its_size(tmp_path):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

    from tests.geometry.parts import _cut, _pocket_body, plain_block

    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(40, 25, 0), gp_Dir(0, 0, 1)), 0.5236)  # 30°
    part = BRepBuilderAPI_Transform(_cut(plain_block(), _pocket_body(20, 12.5, 17, 40, 25, 10, 5)), turn).Shape()
    _, got = _phrases(part, tmp_path, "rotated")
    assert not [p for p in got.values() if p and "pocket" in p], got


def test_a_30_degree_chamfer_is_not_called_a_chamfer(tmp_path):
    """Legs 1.5 mm across and 2.6 mm up the bottom front edge: a real non-45° face, not a faked box."""
    from tests.geometry.parts import _box, _cut, _prism

    corner = _prism([(-1, 2.25, -1.3), (-1, -0.75, 3.9), (-1, -2, -2)], (102, 0, 0))
    f, got = _phrases(_cut(_box(0, 0, 0, 100, 60, 20), corner), tmp_path, "c30")
    face = _group_of(f, lambda x: x.normal and abs(x.normal[1]) > 0.3 and abs(x.normal[2]) > 0.3)
    assert got[face] is None
