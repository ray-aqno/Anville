"""Planner rules on hand-built PartFacts: plan.py is pure Python and needs no STEP file."""

from anville.geometry import Access, Edge, FaceFacts, PartFacts, ShopProfile, Tool, face_groups, min_setups


def _face(fid, access, **kw):
    return FaceFacts(id=fid, surface="plane", area=100.0, centroid=(0.0, 0.0, 0.0), stock=False,
                     access={d: Access(*a) for d, a in access.items()}, **kw)


def _facts(faces, edges=()):
    edges = list(edges)
    return PartFacts(source_sha256="test", bbox=((0, 0, 0), (1, 1, 1)), faces=faces, edges=edges,
                     groups=face_groups(faces, edges), extract_s=0.0)


SHORT = ShopProfile(tools=[Tool("EM6", 6.0, 4.0)])                       # reaches 24 mm
LONG = ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0)])  # reaches 48 mm


def test_reach_is_diameter_times_ld():
    facts = _facts([_face(1, {"+Z": (1.0, 30.0)})])
    short = min_setups(facts, SHORT)
    assert [(u.face_id, u.reason) for u in short.unreachable] == [(1, "no_tool")]
    assert "30" in short.unreachable[0].detail
    long = min_setups(facts, LONG)
    assert long.unreachable == []
    assert [(a.face_id, a.tool) for a in long.setups[0].faces] == [(1, "EM12")]


def test_sharp_inside_corner_parallel_to_tool_fits_nothing():
    walls = [_face(1, {"+Z": (1.0, 10.0, 40.0)}), _face(2, {"+Z": (1.0, 10.0, 40.0)})]
    plan = min_setups(_facts(walls, [Edge(1, 2, "concave", "Z")]), LONG)
    assert {u.face_id for u in plan.unreachable} == {1, 2}
    assert "sharp" in plan.unreachable[0].detail


def test_sharp_corner_across_the_tool_axis_is_fine():
    """A floor-to-wall edge is cut by the end of a flat endmill."""
    walls = [_face(1, {"+Z": (1.0, 10.0, 40.0)}), _face(2, {"+Z": (1.0, 10.0)})]
    plan = min_setups(_facts(walls, [Edge(1, 2, "concave", "X")]), LONG)
    assert plan.unreachable == []


def test_limit_is_inherited_across_the_face_group():
    """Coplanar floor pieces share the tightest neighbour's limit, even when one piece touches no wall."""
    floor_a, floor_b = _face(1, {"+Z": (1.0, 10.0)}), _face(2, {"+Z": (1.0, 10.0)})
    wall = _face(3, {"+Z": (1.0, 10.0, 7.0)})
    edges = [Edge(1, 2, "tangent"), Edge(2, 3, "concave", "X")]
    tools = {a.face_id: a.tool for a in min_setups(_facts([floor_a, floor_b, wall], edges), LONG).setups[0].faces}
    assert tools == {1: "EM6", 2: "EM6", 3: "EM6"}


def test_search_is_minimal_not_greedy():
    """Taking +Z first would need two setups; +X alone covers both."""
    faces = [_face(1, {"+Z": (1.0, 5.0), "+X": (1.0, 5.0)}), _face(2, {"+X": (1.0, 5.0)})]
    plan = min_setups(_facts(faces), LONG)
    assert plan.count == 1
    assert plan.setups[0].directions == ["+X"]


def test_partly_occluded_face_is_not_reachable_from_that_direction():
    plan = min_setups(_facts([_face(1, {"+Z": (0.9, 5.0)})]), LONG)
    assert [(u.face_id, u.reason) for u in plan.unreachable] == [(1, "no_direction")]


def test_five_axis_reaches_everything_but_the_fixture_side():
    faces = [_face(1, {"+Z": (1.0, 5.0)}), _face(2, {"-Z": (1.0, 5.0)}), _face(3, {"+X": (1.0, 5.0)})]
    assert min_setups(_facts(faces), LONG).count == 3
    plan = min_setups(_facts(faces), LONG, axes=5)
    assert plan.count == 1
    assert sorted(plan.setups[0].directions) == ["+X", "+Z", "-Z"]


def test_five_axis_needs_two_fixturings_when_every_side_is_used():
    faces = [_face(i, {d: (1.0, 5.0)}) for i, d in enumerate(["+Z", "-Z", "+X", "-X", "+Y", "-Y"], 1)]
    assert min_setups(_facts(faces), LONG, axes=5).count == 2
