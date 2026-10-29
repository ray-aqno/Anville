"""The demo's shop-side fix (main session, 2026-10-28): the shop BUYS a back-chamfer cutter from a catalog."""

from dataclasses import replace

import pytest

from anville.geometry import (
    BACK_CHAMFER_CATALOG, HARVEY_910480, ShopProfile, SoftJaws, Tool, assess_back_chamfer, evaluate_edits,
)

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
