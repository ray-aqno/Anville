"""World edits (ADR 0002): the model names a kind and a face group, code proposes values and scores them."""

import pytest

from anville.geometry import Access, Edge, FaceFacts, PartFacts, ShopProfile, Tool, evaluate_edits, face_groups
from anville.geometry.edits import feature_faces


def _group_of(facts, face_id):
    return next(label for label, ids in facts.labelled_groups().items() if face_id in ids)


def _options(result, kind):
    return [o for o in result.options if o.kind == kind]


def test_groups_get_stable_labels(facts):
    f = facts("two_pockets")
    labels = f.labelled_groups()
    assert list(labels) == [f"G{i}" for i in range(1, len(f.groups) + 1)]
    assert f.group_faces("G1") == f.groups[0]


def test_feature_spans_walls_and_floor(facts):
    f = facts("deep_slot")
    wall_group = _group_of(f, next(x.id for x in f.faces if x.concave_radius))
    assert feature_faces(f, wall_group) == {x.id for x in f.faces if not x.stock}


def test_raising_corner_radius_lets_the_big_tool_in(facts, shop):
    f = facts("tight_corner")
    corner = next(x.id for x in f.faces if x.concave_radius == pytest.approx(2.0))
    result = evaluate_edits(f, shop, [{"kind": "set_min_radius", "face_group": _group_of(f, corner)}])
    radii = {o.params["radius"]: o for o in _options(result, "set_min_radius")}
    assert sorted(radii) == [3.0, 6.0]  # EM6 and EM12 corners
    best = radii[6.0]
    assert (best.before.setups, best.after.setups) == (1, 1)
    assert {c.face_id: (c.before, c.after) for c in best.tool_changes}[corner] == ("EM3", "EM12")
    assert best.side == "part" and best.basis == "predicted"


def test_radius_edit_leaves_floor_edges_sharp(facts, shop):
    """Floor-to-wall edges run across the tool axis: a flat endmill cuts them, so the edit doesn't round them."""
    f = facts("tight_corner")
    corner = next(x.id for x in f.faces if x.concave_radius == pytest.approx(2.0))
    floor_edges = {(e.a, e.b) for e in f.edges if e.kind == "concave" and e.axis in ("X", "Y")}
    assert floor_edges
    from anville.geometry.edits import _with_radius, feature_faces as feature
    edited = _with_radius(f, feature(f, _group_of(f, corner)), 6.0)
    assert all(e.radius is None for e in edited.edges if (e.a, e.b) in floor_edges)


def test_shallower_slot_becomes_reachable(facts, shop):
    f = facts("deep_slot")
    slot = _group_of(f, next(x.id for x in f.faces if x.concave_radius))
    result = evaluate_edits(f, shop, [{"kind": "set_depth", "face_group": slot}])
    by_depth = {o.params["depth"]: o for o in _options(result, "set_depth")}
    assert by_depth[12.0].params["direction"] == "+Z"  # EM3 reach at L:D 4
    assert (by_depth[12.0].before.unreachable, by_depth[12.0].after.unreachable) == (10, 0)


def test_removing_the_bottom_pocket_drops_a_setup(facts, shop):
    f = facts("two_pockets")
    bottom = next(x.id for x in f.faces if x.concave_radius and x.access.get("-Z"))
    result = evaluate_edits(f, shop, [{"kind": "remove_feature", "face_group": _group_of(f, bottom)}])
    (removal,) = result.options
    assert (removal.before.setups, removal.after.setups) == (2, 1)
    assert len(removal.removed_faces) == 9  # 4 walls, 4 corner fillets, the floor


def test_set_depth_never_proposes_zero(facts, shop):
    """Removal has its own kind: one option, whatever direction the feature is cut from."""
    f = facts("deep_slot")
    slot = _group_of(f, next(x.id for x in f.faces if x.concave_radius))
    depths = [o.params["depth"] for o in evaluate_edits(f, shop, [{"kind": "set_depth", "face_group": slot}]).options]
    assert 0.0 not in depths and depths


def test_long_reach_tool_for_the_slot(facts, shop):
    f = facts("deep_slot")
    slot = _group_of(f, next(x.id for x in f.faces if x.concave_radius))
    (option,) = _options(evaluate_edits(f, shop, [{"kind": "add_tool", "face_group": slot}]), "add_tool")
    assert option.side == "shop"
    assert option.params["diameter"] == 3.0 and option.params["diameter"] * option.params["max_ld"] >= 45.0
    assert option.after.unreachable == 0


def test_naming_two_groups_of_one_feature_offers_each_value_once(facts, shop):
    f = facts("deep_slot")
    groups = sorted({_group_of(f, x.id) for x in f.faces if not x.stock})
    assert len(groups) == 2  # walls and floor
    result = evaluate_edits(f, shop, [{"kind": "set_depth", "face_group": g} for g in groups])
    assert sorted(o.params["depth"] for o in result.options) == [12.0]
    assert [x.reason for x in result.skipped] == ["same feature as an earlier request: its options are listed once"]


def test_five_axis_routing(facts, shop):
    (option,) = evaluate_edits(facts("side_hole"), shop, [{"kind": "route_machine"}]).options
    assert (option.before.setups, option.after.setups, option.params["axes"]) == (3, 1, 5)


def test_fixtures_are_not_modelled_yet(facts, shop):
    result = evaluate_edits(facts("two_pockets"), shop, [{"kind": "add_fixture", "face_group": "G1"}])
    assert result.options == []
    assert "not modelled" in result.skipped[0].reason


def test_edits_never_touch_the_original(facts, shop):
    f = facts("tight_corner")
    snapshot = f.to_dict()
    evaluate_edits(f, shop, [{"kind": "set_min_radius", "face_group": "G1"},
                             {"kind": "set_depth", "face_group": "G1"}])
    assert f.to_dict() == snapshot


@pytest.mark.parametrize("request_, error", [
    ({"kind": "move_hole", "face_group": "G1"}, "catalog"),
    ({"kind": "set_depth", "face_group": "G99"}, "G99"),
    ({"kind": "set_depth"}, "face_group"),
    ({"kind": "set_depth", "face_group": "G1", "depth": 32}, "values"),
])
def test_requests_outside_the_catalog_are_rejected(request_, error, facts, shop):
    with pytest.raises(ValueError, match=error):
        evaluate_edits(facts("two_pockets"), shop, [request_])


def test_filleting_a_sharp_corner_replaces_zero_with_the_radius():
    """Pure facts: two walls meeting at a sharp Z edge become finishable once the edge gets a radius."""
    walls = [FaceFacts(id=i, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                       access={"+Z": Access(1.0, 10.0, 40.0)}) for i in (1, 2)]
    edges = [Edge(1, 2, "concave", "Z")]
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (1, 1, 1)), faces=walls, edges=edges,
                  groups=face_groups(walls, edges), extract_s=0.0)
    shop = ShopProfile([Tool("EM6", 6.0, 4.0)])
    (option,) = evaluate_edits(f, shop, [{"kind": "set_min_radius", "face_group": "G1"}]).options
    assert option.params["radius"] == 3.0
    assert (option.before.unreachable, option.after.unreachable) == (2, 0)


def _chain(edge_order):
    """Three single-face groups joined A-B-C by concave edges, listed in the given order."""
    faces = [FaceFacts(id=i, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                       access={"+Z": Access(1.0, 5.0)}) for i in (1, 2, 3)]
    edges = [Edge(*pair, "concave") for pair in edge_order]
    return PartFacts(source_sha256="t", bbox=((0, 0, 0), (1, 1, 1)), faces=faces, edges=edges,
                     groups=face_groups(faces, edges), extract_s=0.0)


@pytest.mark.parametrize("order", [[(1, 2), (2, 3)], [(2, 3), (1, 2)]])
def test_feature_is_one_hop_whatever_the_edge_order(order):
    f = _chain(order)
    assert feature_faces(f, "G1") == {1, 2}


def test_long_reach_tool_gets_a_name_no_shop_tool_has():
    f = _chain([(1, 2), (2, 3)])
    for face in f.faces:
        face.access["+Z"].depth = 45.0
    shop = ShopProfile([Tool("EM3", 3.0, 4.0), Tool("EM3-LR", 3.0, 8.0)])
    (option,) = evaluate_edits(f, shop, [{"kind": "add_tool", "face_group": "G1"}]).options
    assert option.params["name"] not in {t.name for t in shop.tools}
    assert "45" in option.params["name"]


@pytest.mark.parametrize("kind", ["route_machine", "add_fixture"])
def test_unknown_group_is_rejected_for_every_kind(kind, facts, shop):
    with pytest.raises(ValueError, match="G99"):
        evaluate_edits(facts("two_pockets"), shop, [{"kind": kind, "face_group": "G99"}])


def test_options_that_leave_the_plan_unchanged_are_returned_and_flagged():
    """Removing one of two top features saves no setup; the caller still sees it, and decides."""
    faces = [FaceFacts(id=i, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                       access={"+Z": Access(1.0, 5.0)}) for i in (1, 2)]
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (1, 1, 1)), faces=faces, edges=[],
                  groups=face_groups(faces, []), extract_s=0.0)
    result = evaluate_edits(f, ShopProfile([Tool("EM6", 6.0, 4.0)]), [{"kind": "remove_feature", "face_group": "G1"}])
    (removal,) = result.options
    assert removal.kind == "remove_feature" and removal.removed_faces == [1]
    assert (removal.before, removal.after) == (removal.before, removal.before)
    assert removal.changes_plan is False
    assert result.skipped == []


def test_plan_changing_options_sort_first(facts, shop):
    f = facts("two_pockets")
    result = evaluate_edits(f, shop, [{"kind": k, "face_group": g} for g in f.labelled_groups()
                                      for k in ("set_min_radius", "remove_feature")])
    flags = [o.changes_plan for o in result.options]
    assert flags == sorted(flags, reverse=True) and flags[0]


def test_radius_candidates_survive_a_face_cut_from_another_direction():
    """Walls cut from +Z meet at a sharp Z corner; a neighbour reachable only from +X must not hide the fix."""
    faces = [FaceFacts(id=1, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                       access={"+Z": Access(1.0, 10.0, 40.0)}),
             FaceFacts(id=2, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                       access={"+Z": Access(1.0, 10.0, 40.0)}),
             FaceFacts(id=3, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False,
                       access={"+X": Access(1.0, 5.0)})]
    edges = [Edge(1, 2, "concave", "Z"), Edge(1, 3, "concave")]
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (1, 1, 1)), faces=faces, edges=edges,
                  groups=face_groups(faces, edges), extract_s=0.0)
    result = evaluate_edits(f, ShopProfile([Tool("EM6", 6.0, 4.0)]), [{"kind": "set_min_radius", "face_group": "G1"}])
    (option,) = result.options
    assert option.params["radius"] == 3.0
    assert (option.before.unreachable, option.after.unreachable) == (2, 0)
