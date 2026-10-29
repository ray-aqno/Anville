"""The demo's three routes for Part A, from evaluate_edits' real options, priced with the DLL profile."""

from dataclasses import replace

from anville.geometry import (
    BACK_CHAMFER_CATALOG, SoftJaws, Tool, assess_back_chamfer, evaluate_edits, min_setups,
)
from anville.shop import DLL_COSTS, Shop, cost_options

DLL = Shop(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)], fixture=SoftJaws(),
           name="Marquette DLL", costs=DLL_COSTS)


def _part_a(step, facts):
    f = facts("demo_bracket")
    face = f.edge_chamfers[0].face_id
    group = next(label for label, ids in f.labelled_groups().items() if face in ids)
    access = assess_back_chamfer(step("demo_bracket"), f, replace(DLL, back_chamfer_tools=list(BACK_CHAMFER_CATALOG)))
    requests = [{"kind": "add_tool", "face_group": group}, {"kind": "remove_feature", "face_group": group},
                {"kind": "route_machine"}]
    options = [o for o in evaluate_edits(f, DLL, requests, back_chamfer=access).options if o.changes_plan]
    return f, options, min_setups(f, DLL, back_chamfer=access)


def test_part_a_prices_the_flip_and_the_part_shop_and_outsourced_fixes(step, facts):
    f, options, plan = _part_a(step, facts)
    result = cost_options(f, DLL, options, plan=plan)
    routes = {r.key: r for r in result.routes}
    assert {r.side for r in result.routes} == {"current", "part", "shop", "outsource"}
    assert routes["current"].setups == 2 and all(r.setups == 1 for k, r in routes.items() if k != "current")

    (buy,) = [r for r in result.routes if r.key.endswith("BC45-910480")]
    # Half of $243.50 (shared with Part B) + jaw blanks $20-60 + 0.5-1 h of jaw machining at $40-120
    assert buy.one_time == (161.75, 301.75)
    assert buy.payback is not None and buy.chamfers == 1
    assert [v.quantity for v in result.verdicts] == [4, 10, 20]
    assert result.part == f.source_sha256
