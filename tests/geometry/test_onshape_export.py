"""A real Onshape export (Rayyan, 2026-10-29): a Z bracket of 1/2" plate, 7" × 3" × 2.41".

The web carries a Ø1.75" bore and two 3/8" holes; each flange carries two side holes. It is the first
part not built in code: AP242 from ST-Developer, in metres, with inch-fraction radii.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from anville.geometry import (
    Access, FaceFacts, PartFacts, ShopProfile, Tool, evaluate_edits, extract, face_groups, min_setups,
)

STEP = Path(__file__).parent / "data" / "onshape_z_bracket.step"
WEB_HOLES = {11, 13}  # the two 3/8" holes through the 12.7 mm web


@pytest.fixture(scope="module")
def bracket():
    return extract(STEP)


def test_metres_are_read_as_millimetres(bracket):
    lo, hi = bracket.bbox
    assert [h - l for l, h in zip(lo, hi)] == pytest.approx([7 * 25.4, 3 * 25.4, 61.239], abs=1e-3)


def test_faces_and_holes(bracket):
    assert len(bracket.faces) == 17 and bracket.edge_chamfers == []
    radii = sorted(f.radius for f in bracket.faces if f.surface == "cylinder")
    assert radii == pytest.approx([4.7625, 4.7625, 6.3179, 6.35, 6.35, 6.6921, 22.225], abs=1e-4)
    # The Z bend's two inside corners are sharp and run along Y: they limit nothing cut from ±Z.
    assert sorted((e.kind, e.axis, e.radius) for e in bracket.edges if e.kind == "concave") \
        == [("concave", "Y", None)] * 2


def test_setups(bracket, shop):
    three = min_setups(bracket, shop)
    assert sorted(s.directions[0] for s in three.setups) == ["+X", "+Z", "-X", "-Z"]
    assert min_setups(bracket, shop, axes=5).count == 1


# ---- ADR 0008 Amendment 1: reach depends on the holder ------------------------------------------

@pytest.mark.parametrize("holder,stickout", [(50, 12.7), (67, 12.7), (70, 38.1)])
def test_reach_lookup_takes_the_last_step_the_holder_meets(holder, stickout):
    """The amendment's worked table: hole F13 from +Z, EM6 (r = 3), sweep = R - r + 1 mm."""
    a = Access(1.0, 38.1, reach=[[0.0, 12.7], [31.7, 38.1]])
    assert a.stickout(holder / 2 - 3 + 1) == stickout


def test_no_curve_means_the_stock_top_depth():
    assert Access(1.0, 8.0).stickout(0.0) == Access(1.0, 8.0).stickout(500.0) == 8.0


def test_web_holes_get_a_curve_from_both_sides(bracket):
    by_id = {f.id: f for f in bracket.faces}
    # The flanges' inner walls are thin in plan view, so their B-rep boxes set the step: 31.76 mm, floored.
    assert by_id[13].access["+Z"].reach == [[0.0, 12.7], [31.7, 38.1]]  # the -X flange rises 31.7 mm away
    assert by_id[11].access["-Z"].reach == [[0.0, 12.7], [31.7, 35.8389]]  # the +X flange hangs 31.7 mm away
    for f in bracket.faces:
        for a in f.access.values():  # the curve ends at the worst case, so depth keeps its meaning
            assert a.reach[0][0] == 0.0 and a.reach[-1][1] == pytest.approx(a.depth, abs=0.06)


def test_web_holes_are_reachable(bracket, shop):
    plan = min_setups(bracket, shop)
    assert plan.unreachable == []
    tools = {a.face_id: (s.directions[0], a.tool) for s in plan.setups for a in s.faces}
    assert {fid: tools[fid] for fid in WEB_HOLES} == {11: ("+Z", "EM6"), 13: ("+Z", "EM6")}


def test_the_holder_decides_which_side_drills_the_hole(bracket, shop):
    """A Ø70 holder (sweep 33 mm) meets the -X flange from +Z (31.7 mm), so F13 is drilled from -Z, where the
    nearest taller material is the side face's box at 33.3 mm. Same setup count: -Z is needed anyway."""
    plan = min_setups(bracket, replace(shop, holder_diameter=70.0))
    tools = {a.face_id: (s.directions[0], a.tool) for s in plan.setups for a in s.faces}
    assert plan.unreachable == [] and plan.count == 4
    assert tools[13] == ("-Z", "EM6") and tools[11] == ("+Z", "EM6")


def _web_hole_alone():
    """F13 as if only +Z could reach it: its measured curve, its r4.7625 wall."""
    face = FaceFacts(id=1, surface="cylinder", area=380.0, centroid=(0, 0, 0), stock=False,
                     access={"+Z": Access(1.0, 38.1, 9.53, reach=[[0.0, 12.7], [31.7, 38.1]])},
                     concave_radius=4.7625, concave_axis="Z")
    return PartFacts(source_sha256="t", bbox=((0, 0, 0), (100, 60, 40)), faces=[face], edges=[],
                     groups=face_groups([face], []), extract_s=0.0)


def test_a_wide_holder_says_both_ends_of_the_curve(shop):
    (u,) = min_setups(_web_hole_alone(), replace(shop, holder_diameter=70.0)).unreachable
    assert "reaching 38.1 mm" in u.detail and "12.7 mm with a holder whose edge stops short of 31.7 mm from the face" in u.detail


def test_a_slimmer_holder_is_offered_next_to_a_longer_tool(shop):
    result = evaluate_edits(_web_hole_alone(), replace(shop, holder_diameter=70.0),
                            [{"kind": "add_tool", "face_group": "G1"}])
    by_key = {o.key: o for o in result.options}
    slim = by_key["add_tool/G1/+Z/EM6-H67"]  # the amendment's "up to Ø67": sweep 31.5 < 31.7
    assert slim.side == "shop" and slim.params == {"type": "holder", "name": "EM6", "holder_diameter": 67,
                                                   "direction": "+Z"}
    assert (slim.before.unreachable, slim.after.unreachable) == (1, 0)
    longer = by_key["add_tool/G1/+Z/EM6-L42"]  # 38.1 mm from a Ø70 holder needs 7 × D
    assert longer.after.unreachable == 0
    assert min_setups(_web_hole_alone(), shop).unreachable == []  # the Ø50 default already clears


def test_curves_survive_json(bracket):
    assert PartFacts.from_dict(json.loads(json.dumps(bracket.to_dict()))) == bracket


# ---- Codex review of the holder-reach branch -------------------------------------------------

def test_a_wall_thinner_than_the_grid_still_counts(facts):
    """The 0.2 mm wall rises 2 mm right beside the step: without it, the curve says 0 mm up to the far wall."""
    f = facts("thin_wall_step")
    step = next(x for x in f.faces if x.normal == (0.0, 0.0, 1.0) and x.centroid[0] < 10 and x.centroid[2] == 10)
    reach = step.access["+Z"].reach
    assert reach[0] == [0.0, pytest.approx(2.0)] and reach[-1][1] == pytest.approx(step.access["+Z"].depth)


def test_a_small_last_rise_is_kept():
    """Obstacles needing 2, 5 and 5.04 mm at 0.5, 5 and 10 mm: the curve must end at 5.04, not 5."""
    from anville.geometry.step import _reach
    reach = _reach([(0.5, 0.0, 2.0), (5.0, 0.0, 5.0), (10.0, 0.0, 5.04)], (0, 0, 0), (0, 0, 0), "+Z", 0.0, 5.04)
    assert reach[-1][1] == pytest.approx(5.04)
    assert Access(1.0, 5.04, reach=reach).stickout(12.0) == pytest.approx(5.04)


def test_set_depth_never_rounds_short_of_the_tool():
    """Stick-out 8 mm, reach 6.99996 mm: the shift is 1.00004 mm, and rounding the depth to 9.0 would lose it."""
    face = FaceFacts(id=1, surface="plane", area=100.0, centroid=(0, 0, 0), stock=False, normal=(0.0, 0.0, 1.0),
                     access={"+Z": Access(1.0, 10.0, reach=[[0.0, 8.0], [30.0, 10.0]])})
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (50, 50, 20)), faces=[face], edges=[],
                  groups=face_groups([face], []), extract_s=0.0)
    shop = ShopProfile(tools=[Tool("T1", 1.0, 6.99996)])
    (option,) = evaluate_edits(f, shop, [{"kind": "set_depth", "face_group": "G1"}]).options
    assert option.after.unreachable == 0
