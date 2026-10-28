"""Oracle tests: synthetic parts with known setup counts (ADR 0002 'Harder': the planner needs them)."""

import json

import pytest

from anville.geometry import PartFacts, min_setups
from tests.geometry.parts import PARTS

# name -> (setup count, directions per setup) on a 3-axis mill
EXPECTED_3AXIS = {
    "plain_block": (1, [["+Z"]]),
    "two_pockets": (2, [["+Z"], ["-Z"]]),
    "side_hole": (3, [["+Z"], ["-Z"], ["+X"]]),
    "deep_slot": (1, [["+Z"]]),
    "tight_corner": (1, [["+Z"]]),
    "clevis": (1, [["+Z"]]),
    "undercut": (1, [["+Z"]]),
    "wedge": (1, [["-Z"]]),
    "kinked_pocket": (1, [["+Z"]]),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_3AXIS))
def test_exact_setup_count(name, facts, shop):
    plan = min_setups(facts(name), shop)
    count, directions = EXPECTED_3AXIS[name]
    assert plan.count == count
    assert [s.directions for s in plan.setups] == directions


def test_side_hole_adds_exactly_one_setup(facts, shop):
    assert min_setups(facts("side_hole"), shop).count == min_setups(facts("two_pockets"), shop).count + 1


def test_side_hole_faces_go_to_the_plus_x_setup(facts, shop):
    f = facts("side_hole")
    plan = min_setups(f, shop)
    hole = {face.id for face in f.faces if face.concave_radius == pytest.approx(4.0)}
    assert len(hole) == 1
    assert hole <= {a.face_id for a in plan.setups[2].faces}


def test_plain_block_is_all_stock(facts):
    assert all(face.stock for face in facts("plain_block").faces)


def test_deep_slot_is_unreachable_because_no_tool_fits(facts, shop):
    f = facts("deep_slot")
    plan = min_setups(f, shop)
    machined = {face.id for face in f.faces if not face.stock}
    assert len(machined) == 10
    # Every slot face, including the coplanar floor slivers left by the boolean (ADR 0008 §5 regression).
    assert {u.face_id for u in plan.unreachable} == machined
    assert {u.reason for u in plan.unreachable} == {"no_tool"}


def test_tight_corner_forces_the_small_tool(facts, shop):
    f = facts("tight_corner")
    tools = {a.face_id: a.tool for a in min_setups(f, shop).setups[0].faces}
    corners = [face.id for face in f.faces if face.concave_radius == pytest.approx(2.0)]
    assert len(corners) == 4
    assert {tools[c] for c in corners} == {"EM3"}


def test_wider_corners_allow_the_bigger_tool(facts, shop):
    f = facts("two_pockets")
    tools = {a.face_id: a.tool for s in min_setups(f, shop).setups for a in s.faces}
    corners = [face.id for face in f.faces if face.concave_radius == pytest.approx(5.0)]
    assert len(corners) == 8
    assert {tools[c] for c in corners} == {"EM6"}


def test_clevis_gap_caps_the_tool_without_any_radius(facts, shop):
    f = facts("clevis")
    assert all(face.concave_radius is None for face in f.faces)
    tools = {a.tool for a in min_setups(f, shop).setups[0].faces}
    assert tools == {"EM6"}


def test_bore_inside_pocket_wall_has_no_direction(facts, shop):
    plan = min_setups(facts("undercut"), shop)
    assert len(plan.unreachable) == 2
    assert {u.reason for u in plan.unreachable} == {"no_direction"}


def test_slightly_sloped_face_is_not_stock(facts):
    f = facts("wedge")
    assert [face.normal[2] < -0.999 for face in f.faces if not face.stock] == [True]


def test_shallow_sharp_kink_is_not_treated_as_tangent(facts, shop):
    f = facts("kinked_pocket")
    kink = [e for e in f.edges if e.axis == "Z"]
    assert len(kink) == 1 and kink[0].kind == "concave"
    flagged = {u.face_id: u for u in min_setups(f, shop).unreachable}
    assert {kink[0].a, kink[0].b} <= flagged.keys()
    assert "sharp" in flagged[kink[0].a].detail


@pytest.mark.parametrize("name,count", [("plain_block", 1), ("two_pockets", 1), ("side_hole", 1), ("undercut", 1)])
def test_five_axis_needs_one_fixturing(name, count, facts, shop):
    plan = min_setups(facts(name), shop, axes=5)
    assert plan.count == count


def test_five_axis_does_not_reach_the_undercut(facts, shop):
    plan = min_setups(facts("undercut"), shop, axes=5)
    assert {u.reason for u in plan.unreachable} == {"no_direction"}


@pytest.mark.parametrize("name", sorted(PARTS))
def test_part_facts_round_trip_through_json(name, facts):
    f = facts(name)
    assert PartFacts.from_dict(json.loads(json.dumps(f.to_dict()))) == f


@pytest.mark.parametrize("name", sorted(PARTS))
def test_extraction_is_fast(name, facts):
    assert facts(name).extract_s < 5.0
