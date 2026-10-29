"""RFC 0002: the exterior-edge-chamfer recognizer (§1) and the back-chamfer access rule, checks a-f (§2-3)."""

from dataclasses import replace

import pytest

from anville.geometry import (
    HARVEY_910480, Access, EdgeChamfer, FaceFacts, PartFacts, ShopProfile, SoftJaws, Tool, assess_back_chamfer,
    face_groups, min_setups,
)

MILLS = [Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)]
DEMO_SHOP = ShopProfile(tools=MILLS, back_chamfer_tools=[HARVEY_910480], fixture=SoftJaws())


def _assess(step, facts, name, shop=DEMO_SHOP):
    (access,) = assess_back_chamfer(step(name), facts(name), shop)
    return access


def _check(access, name):
    return next(c for c in access.checks if c.name == name)


# ---- §1 recognizer ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,axis,outward", [("demo_bracket", "X", "-Y"), ("demo_bracket_b", "Y", "-X")])
def test_recognizes_the_bottom_edge_chamfer(name, axis, outward, facts):
    f = facts(name)
    (ch,) = f.edge_chamfers
    face = next(x for x in f.faces if x.id == ch.face_id)
    assert (ch.axis, ch.outward) == (axis, outward)
    assert ch.leg == pytest.approx(1.5) and ch.angle == pytest.approx(45.0)
    assert {ch.side_face, ch.bottom_face} <= set(face.stock_neighbors)


def test_top_edge_break_is_not_an_exterior_bottom_chamfer(facts):
    """Part A's 1 mm top edge break faces up: it bridges the top, not the bottom."""
    f = facts("demo_bracket")
    top_break = next(x for x in f.faces if x.normal and x.normal[2] > 0.6)
    assert top_break.id not in {c.face_id for c in f.edge_chamfers}


@pytest.mark.parametrize("name", ["plain_block", "two_pockets", "wedge", "demo_bracket_unchamfered"])
def test_no_chamfer_no_recognition(name, facts):
    """Drafted or sloped faces don't bridge a stock side and the stock bottom at 45°."""
    assert facts(name).edge_chamfers == []


def test_leg_is_measured_across_the_edge_not_along_it(facts):
    """Part B's chamfer is 40 mm long and 2.12 mm wide: the leg is width / √2, never length or diagonal."""
    (ch,) = facts("demo_bracket_b").edge_chamfers
    assert ch.leg == pytest.approx(1.5, abs=1e-3)


# ---- the demo path ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["demo_bracket", "demo_bracket_b"])
def test_demo_parts_pass_every_check_with_910480_and_relieved_jaws(name, step, facts):
    access = _assess(step, facts, name)
    assert [c.name for c in access.checks] == list("abcdef")
    assert access.ok, [c for c in access.checks if not c.passed]
    assert _check(access, "c").margin == pytest.approx(0.825, abs=1e-3)


@pytest.mark.parametrize("name", ["demo_bracket", "demo_bracket_b"])
def test_part_with_910480_and_relieved_jaws_needs_one_setup(name, step, facts):
    f = facts(name)
    access = assess_back_chamfer(step(name), f, DEMO_SHOP)
    plan = min_setups(f, DEMO_SHOP, back_chamfer=access)
    assert [s.directions for s in plan.setups] == [["+Z"]]
    tools = {a.face_id: a.tool for a in plan.setups[0].faces}
    assert tools[f.edge_chamfers[0].face_id] == HARVEY_910480.name


def test_without_the_cutter_the_flip_stays(facts):
    shop = ShopProfile(tools=MILLS, fixture=SoftJaws())
    assert min_setups(facts("demo_bracket"), shop, back_chamfer=[]).count == 2


def test_swept_neck_clearance_agrees_with_check_c(step, facts):
    """Check e measures the swept neck's real distance to the part; check c computes it from the catalog."""
    access = _assess(step, facts, "demo_bracket")
    assert _check(access, "e").margin == pytest.approx(_check(access, "c").margin, abs=0.02)


def test_jaw_reliefs_are_reported_for_the_setup_sheet(step, facts):
    access = _assess(step, facts, "demo_bracket")
    assert len(access.reliefs) == 2  # one per jaw, both ends of the edge
    for r in access.reliefs:
        assert r.size[0] > 0 and r.size[1] > 0 and r.size[2] > 0
        assert r.contact_kept > 0.9  # the relief sits outboard of the part, not under the grip


# ---- negatives: each says which check failed and by how much ---------------------------------

def test_two_mm_chamfer_fails_neck_clearance_by_0p18(step, facts):
    access = _assess(step, facts, "demo_bracket_2mm")
    c = _check(access, "c")
    assert not c.passed and c.margin == pytest.approx(0.325, abs=1e-3)  # needs 0.5: short by 0.175
    assert "0.175" in c.detail or "0.18" in c.detail
    plan = min_setups(facts("demo_bracket_2mm"), DEMO_SHOP, back_chamfer=[access])
    assert plan.count == 2


@pytest.mark.parametrize("change,check", [
    ({"angle": 60.0}, "a"),
    ({"length_of_cut": 1.0}, "b"),
    ({"neck_length": 20.0}, "d"),
])
def test_cutter_that_does_not_match_fails_its_check(change, check, step, facts):
    shop = replace(DEMO_SHOP, back_chamfer_tools=[replace(HARVEY_910480, **change)])
    access = _assess(step, facts, "demo_bracket", shop)
    assert not access.ok and [c.name for c in access.checks if not c.passed] == [check]


def test_chamfer_split_by_a_slot_is_recognized_at_full_length(facts):
    """One notched chamfer face bordering two bottom faces: sweep its whole length, not one shared edge."""
    (ch,) = facts("split_edge_chamfer").edge_chamfers
    assert sorted((ch.start[0], ch.end[0])) == pytest.approx([0.0, 100.0])


def test_overtravel_into_a_sharp_corner_fails_the_swept_part_check(step, facts):
    access = _assess(step, facts, "stopped_chamfer")
    e = _check(access, "e")
    assert not e.passed and e.margin is None and "mm³" in e.detail


def test_unrelieved_jaws_collide(step, facts):
    shop = replace(DEMO_SHOP, fixture=SoftJaws(relieved=False))
    f = _check(_assess(step, facts, "demo_bracket", shop), "f")
    assert not f.passed and "jaw" in f.detail


def test_parallels_under_the_chamfer_collide(step, facts):
    shop = replace(DEMO_SHOP, fixture=SoftJaws(parallel_setback=0.0))
    f = _check(_assess(step, facts, "demo_bracket", shop), "f")
    assert not f.passed and "parallel" in f.detail


def test_no_fixture_stated_fails_workholding(step, facts):
    shop = replace(DEMO_SHOP, fixture=None)
    f = _check(_assess(step, facts, "demo_bracket", shop), "f")
    assert not f.passed and "fixture" in f.detail


# ---- Greptile on PR #15 ----------------------------------------------------------------------

class _Pass:
    """A stand-in for a passing BackChamferAccess (the planner reads face_id, tool and ok)."""

    def __init__(self, face_id):
        self.face_id, self.tool, self.ok = face_id, HARVEY_910480.name, True


def test_chamfers_needing_different_jaws_cannot_share_one_setup():
    """Jaws grip across the chamfered edge, so an X-edge and a Y-edge chamfer need different fixtures."""
    def face(fid, d):
        return FaceFacts(id=fid, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                         access={d: Access(1.0, 5.0)})
    faces = [face(1, "+Z"), face(2, "-Z"), face(3, "-Z")]
    chamfers = [EdgeChamfer(2, 9, 9, "X", "-Y", 45.0, 1.5, (0, 1.5, 0), (100, 1.5, 0)),
                EdgeChamfer(3, 9, 9, "Y", "-X", 45.0, 1.5, (1.5, 0, 0), (1.5, 60, 0))]
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (100, 60, 20)), faces=faces, edges=[],
                  groups=face_groups(faces, []), extract_s=0.0, edge_chamfers=chamfers)
    plan = min_setups(f, DEMO_SHOP, back_chamfer=[_Pass(2), _Pass(3)])
    assert plan.count == 2
    back_chamfered = [a.face_id for s in plan.setups for a in s.faces if a.tool == HARVEY_910480.name]
    assert len(back_chamfered) == 1  # one jaw orientation per setup


def test_chamfers_on_one_axis_are_checked_against_one_fixture(step, facts):
    """Both long edges: each jaw gets both reliefs, both parallels are checked, and it is still 1 setup."""
    f = facts("both_long_chamfers")
    access = assess_back_chamfer(step("both_long_chamfers"), f, DEMO_SHOP)
    assert len(access) == 2 and all(a.ok for a in access)
    assert all(len(a.reliefs) == 4 for a in access)  # 2 jaws × 2 chamfer corners, shared by both
    assert min_setups(f, DEMO_SHOP, back_chamfer=access).count == 1


def test_parallel_must_be_under_the_part(step, facts):
    f = _check(_assess(step, facts, "narrow_bar"), "f")
    assert not f.passed and "parallel" in f.detail and "under the part" in f.detail


@pytest.mark.parametrize("fixture", [SoftJaws(min_grip=0.99), SoftJaws(relief_margin=60.0)])
def test_reliefs_must_leave_enough_grip(fixture, step, facts):
    shop = replace(DEMO_SHOP, fixture=fixture)
    f = _check(_assess(step, facts, "demo_bracket", shop), "f")
    assert not f.passed and "grip" in f.detail


def test_a_complete_plan_beats_a_shorter_incomplete_one():
    """The X chamfer can also be milled from -Z; the Y chamfer has only the back-chamfer cutter. Picking the
    X jaws gives 1 setup with the Y chamfer unfinished; picking the Y jaws finishes everything in 2."""
    def face(fid, access):
        return FaceFacts(id=fid, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False, access=access)
    faces = [face(1, {"+Z": Access(1.0, 5.0)}), face(2, {"-Z": Access(1.0, 1.0)}), face(3, {})]
    chamfers = [EdgeChamfer(2, 9, 9, "X", "-Y", 45.0, 1.5, (0, 1.5, 0), (100, 1.5, 0)),
                EdgeChamfer(3, 9, 9, "Y", "-X", 45.0, 1.5, (1.5, 0, 0), (1.5, 60, 0))]
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (100, 60, 20)), faces=faces, edges=[],
                  groups=face_groups(faces, []), extract_s=0.0, edge_chamfers=chamfers)
    plan = min_setups(f, DEMO_SHOP, back_chamfer=[_Pass(2), _Pass(3)])
    assert plan.unreachable == []
    assert sorted(s.directions[0] for s in plan.setups) == ["+Z", "-Z"]
    tools = {a.face_id: a.tool for s in plan.setups for a in s.faces}
    assert tools[3] == HARVEY_910480.name and tools[2] != HARVEY_910480.name
