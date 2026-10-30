"""S8 shop ledger seed: after Part A the shop owns the 910480, so Part B plans in one setup with it."""

import json
from dataclasses import replace

import pytest

from anville.geometry import (
    BACK_CHAMFER_CATALOG, HARVEY_910480, ShopProfile, SoftJaws, Tool, assess_back_chamfer, evaluate_edits,
    min_setups,
)
from anville.shop.ledger import TOOL_ACQUIRED, apply_ledger, tool_acquired

SHOP = ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)], fixture=SoftJaws())
REVIEW, DECISION = "review:part-a/v3", "comment:c42"


def _buy_option(step, facts, name):
    f = facts(name)
    face = f.edge_chamfers[0].face_id
    group = next(label for label, ids in f.labelled_groups().items() if face in ids)
    access = assess_back_chamfer(step(name), f, replace(SHOP, back_chamfer_tools=list(BACK_CHAMFER_CATALOG)))
    result = evaluate_edits(f, SHOP, [{"kind": "add_tool", "face_group": group}], back_chamfer=access)
    return next(o for o in result.options if o.params.get("type") == "back_chamfer")


def _as_stored(entry):
    """What Store.read_ledger hands back: JSON round-tripped, plus the row's own fields."""
    return {"id": 1, "ledger": "shop_outcome", "scope": "shop:dll", "created_at": 0.0,
            **json.loads(json.dumps(entry))}


def test_part_a_buying_the_cutter_makes_part_b_one_setup(step, facts):
    entry = tool_acquired(_buy_option(step, facts, "demo_bracket"), review=REVIEW, decision=DECISION)
    b = facts("demo_bracket_b")
    assert min_setups(b, SHOP).count == 2  # before the ledger: the flip

    shop, sources = apply_ledger(SHOP, [_as_stored(entry)])
    access = assess_back_chamfer(step("demo_bracket_b"), b, shop)
    plan = min_setups(b, shop, back_chamfer=access)

    assert plan.count == 1 and plan.unreachable == []
    assert b.edge_chamfers[0].face_id in {a.face_id for s in plan.setups for a in s.faces if a.tool == "910480"}
    assert sources["910480"] == {"review": REVIEW, "decision": DECISION, "option": entry["provenance"]["option"]}


def test_the_entry_carries_the_cutter_and_where_it_came_from(step, facts):
    option = _buy_option(step, facts, "demo_bracket")
    entry = tool_acquired(option, review=REVIEW, decision=DECISION)
    assert entry["payload"]["kind"] == TOOL_ACQUIRED and entry["payload"]["type"] == "back_chamfer"
    assert entry["payload"]["tool"]["name"] == "910480"
    assert entry["payload"]["tool"]["head_diameter"] == HARVEY_910480.head_diameter
    assert entry["provenance"] == {"review": REVIEW, "decision": DECISION, "option": option.key}


def test_the_ledger_rebuilds_the_same_cutter():
    entry = {"payload": {"kind": TOOL_ACQUIRED, "type": "back_chamfer",
                         "tool": {"name": "910480", "angle": 45.0, "head_diameter": 12.7, "neck_diameter": 8.05,
                                  "neck_length": 63.5, "length_of_cut": 2.24}},
             "provenance": {"review": REVIEW, "decision": DECISION, "option": "x"}}
    shop, _ = apply_ledger(SHOP, [entry])
    assert shop.back_chamfer_tools == [HARVEY_910480]


def test_a_cutter_already_owned_is_not_added_twice_and_keeps_its_first_source():
    first = {"payload": {"kind": TOOL_ACQUIRED, "type": "back_chamfer", "tool": _tool_dict()},
             "provenance": {"review": "r1", "decision": "d1", "option": "o"}}
    again = {**first, "provenance": {"review": "r2", "decision": "d2", "option": "o"}}
    shop, sources = apply_ledger(SHOP, [first, again])
    assert [t.name for t in shop.back_chamfer_tools] == ["910480"] and sources["910480"]["review"] == "r1"


def test_a_cutter_the_shop_already_had_is_not_credited_to_the_ledger():
    owned = replace(SHOP, back_chamfer_tools=[HARVEY_910480])
    entry = {"payload": {"kind": TOOL_ACQUIRED, "type": "back_chamfer", "tool": _tool_dict()},
             "provenance": {"review": "r1", "decision": "d1", "option": "o"}}
    shop, sources = apply_ledger(owned, [entry])
    assert shop.back_chamfer_tools == [HARVEY_910480] and sources == {}


def test_other_shop_outcomes_are_left_alone():
    shop, sources = apply_ledger(SHOP, [{"payload": {"setups": 3}, "provenance": {"event": "shop:n1"}}])
    assert shop == SHOP and sources == {}


def test_only_a_purchase_becomes_an_acquisition(step, facts):
    option = _buy_option(step, facts, "demo_bracket")
    for not_bought in (replace(option, params={**option.params, "owned": True}),
                       replace(option, kind="route_machine", params={"axes": 5})):
        with pytest.raises(ValueError):
            tool_acquired(not_bought, review=REVIEW, decision=DECISION)


@pytest.mark.parametrize("review, decision", [("", DECISION), (REVIEW, "")])
def test_an_acquisition_needs_both_the_review_and_the_decision(step, facts, review, decision):
    with pytest.raises(ValueError, match="provenance"):
        tool_acquired(_buy_option(step, facts, "demo_bracket"), review=review, decision=decision)


def _tool_dict():
    return {"name": "910480", "angle": 45.0, "head_diameter": 12.7, "neck_diameter": 8.05,
            "neck_length": 63.5, "length_of_cut": 2.24}
