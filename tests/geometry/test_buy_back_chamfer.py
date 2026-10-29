"""The demo's shop-side fix (main session, 2026-10-28): the shop BUYS a back-chamfer cutter from a catalog."""

from dataclasses import replace

import pytest

from anville.geometry import (
    BACK_CHAMFER_CATALOG, HARVEY_910480, Access, EdgeChamfer, FaceFacts, PartFacts, ShopProfile, SoftJaws, Tool,
    assess_back_chamfer, evaluate_edits, face_groups,
)
from anville.geometry.backchamfer import Relief

MILLS = [Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)]
SHOP = ShopProfile(tools=MILLS, fixture=SoftJaws())  # owns no back-chamfer cutter


def _catalog_access(step, facts, name):
    """What the tool layer runs before evaluate_edits: every catalog cutter, checked against this part."""
    return assess_back_chamfer(step(name), facts(name), replace(SHOP, back_chamfer_tools=list(BACK_CHAMFER_CATALOG)))


def _chamfer_group(f):
    face = f.edge_chamfers[0].face_id
    return next(label for label, ids in f.labelled_groups().items() if face in ids)


@pytest.mark.parametrize("name", ["demo_bracket", "demo_bracket_b"])
def test_buying_910480_is_offered_and_drops_the_flip(name, step, facts):
    f = facts(name)
    group = _chamfer_group(f)
    result = evaluate_edits(f, SHOP, [{"kind": "add_tool", "face_group": group}],
                            back_chamfer=_catalog_access(step, facts, name))
    (option,) = [o for o in result.options if o.params.get("type") == "back_chamfer"]
    assert option.key == f"add_tool/{group}/+Z/BC45-910480"
    assert option.side == "shop" and option.changes_plan
    assert (option.before.setups, option.after.setups) == (2, 1)
    assert option.params["owned"] is False and option.params["name"] == HARVEY_910480.name
    assert option.params["head_diameter"] == HARVEY_910480.head_diameter
    assert len(option.params["reliefs"]) == 2


def test_it_sits_next_to_five_axis_routing(step, facts):
    f = facts("demo_bracket")
    result = evaluate_edits(f, SHOP, [{"kind": "add_tool", "face_group": _chamfer_group(f)},
                                      {"kind": "route_machine"}],
                            back_chamfer=_catalog_access(step, facts, "demo_bracket"))
    keys = {o.key for o in result.options}
    assert "route_machine/5" in keys and any(k.endswith("/+Z/BC45-910480") for k in keys)


def test_not_offered_when_the_shop_already_owns_it(step, facts):
    f = facts("demo_bracket")
    owned = replace(SHOP, back_chamfer_tools=[HARVEY_910480])
    result = evaluate_edits(f, owned, [{"kind": "add_tool", "face_group": _chamfer_group(f)}],
                            back_chamfer=_catalog_access(step, facts, "demo_bracket"))
    assert not [o for o in result.options if o.params.get("type") == "back_chamfer"]


def test_a_failing_cutter_is_not_offered_and_says_why(step, facts):
    """Part A at 2 mm: 910480's neck would clear the wall by 0.325 mm, short of 0.5."""
    f = facts("demo_bracket_2mm")
    result = evaluate_edits(f, SHOP, [{"kind": "add_tool", "face_group": _chamfer_group(f)}],
                            back_chamfer=_catalog_access(step, facts, "demo_bracket_2mm"))
    assert not [o for o in result.options if o.params.get("type") == "back_chamfer"]
    (skip,) = [s for s in result.skipped if s.kind == "add_tool"]
    assert "910480" in skip.reason and "check c" in skip.reason and "0.175" in skip.reason


def test_without_assessments_nothing_back_chamfer_is_offered(facts):
    f = facts("demo_bracket")
    result = evaluate_edits(f, SHOP, [{"kind": "add_tool", "face_group": _chamfer_group(f)}])
    assert not [o for o in result.options if o.params.get("type") == "back_chamfer"]


class _Pass:
    """A passing catalog-cutter assessment whose reliefs are tagged with the jaw axis they were checked for."""

    def __init__(self, face_id, axis):
        self.face_id, self.tool, self.ok, self.checks = face_id, HARVEY_910480.name, True, []
        self.reliefs = [Relief(jaw, (0, 0, 0), (1, 1, 1), 0.9) for jaw in (f"{axis}-start", f"{axis}-end")]


def test_reliefs_match_the_jaws_of_the_scored_plan():
    """Greptile, PR #18: X and Y chamfers both pass and tie, and ties go to X. The option for the Y chamfer
    must be scored with the Y jaws its reliefs were checked for, not with the X jaws."""
    def face(fid, access):
        return FaceFacts(id=fid, surface="plane", area=1.0, centroid=(0, 0, 0), stock=False, access=access)
    faces = [face(1, {"+Z": Access(1.0, 5.0)}), face(2, {}), face(3, {})]  # 2, 3: back-chamfer only
    chamfers = [EdgeChamfer(2, 9, 9, "X", "-Y", 45.0, 1.5, (0, 1.5, 0), (100, 1.5, 0)),
                EdgeChamfer(3, 9, 9, "Y", "-X", 45.0, 1.5, (1.5, 0, 0), (1.5, 60, 0))]
    f = PartFacts(source_sha256="t", bbox=((0, 0, 0), (100, 60, 20)), faces=faces, edges=[],
                  groups=face_groups(faces, []), extract_s=0.0, edge_chamfers=chamfers)
    y_group = next(label for label, ids in f.labelled_groups().items() if 3 in ids)
    result = evaluate_edits(f, SHOP, [{"kind": "add_tool", "face_group": y_group}],
                            back_chamfer=[_Pass(2, "X"), _Pass(3, "Y")])
    (option,) = [o for o in result.options if o.params.get("type") == "back_chamfer"]
    assert {r["jaw"][0] for r in option.params["reliefs"]} == {"Y"}
    cut = {c.face_id for c in option.tool_changes if c.after == HARVEY_910480.name}
    assert cut == {3}  # the cuts scored are the ones the reliefs were checked for
