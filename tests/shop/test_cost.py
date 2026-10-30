"""cost_options (RFC 0002 §4): dollars per option at fixed quantities, decided over every corner of the input box."""

import random
from dataclasses import replace

import pytest

from anville.geometry import PartFacts
from anville.geometry.edits import Option, Summary, ToolChange
from anville.geometry.plan import Assignment, Setup, SetupPlan, Unreachable
from anville.shop import DLL_COSTS, Input, Scenarios, Shop, ShopCosts, cost_options

FACTS = PartFacts(source_sha256="part-a", bbox=((0, 0, 0), (100, 60, 20)), faces=[], edges=[], groups=[],
                  extract_s=0.0)


def fixed(v, unit="$"):
    return Input(v, v, unit, "placeholder", "test")


# Every input pinned, so each route's dollars can be derived by hand.
COSTS = ShopCosts(
    mill_rate=fixed(60.0, "$/hr"), setup_time=fixed(0.5, "hr"), load_time=fixed(0.1, "hr"),
    cycle_time=fixed(0.5, "hr"), back_chamfer_pass=fixed(0.05, "hr"), jaw_blank=fixed(40.0),
    jaw_machining=fixed(0.5, "hr"), tool_price={"910480": Input(243.5, 243.5, "$", "verified", "harveytool.com")},
    long_reach_price=fixed(80.0), outsource_rate=fixed(150.0, "$/hr"), outsource_fee=fixed(100.0),
    scenarios=Scenarios(quantities=(10,), batch_size=10, status="placeholder", source="test"),
)
SHOP = Shop(tools=[], name="test shop", costs=COSTS)
# The current plan: the chamfer (face 7) forces a flip.
PLAN = SetupPlan(3, [Setup(["+Z"], [Assignment(1, "EM12")]), Setup(["-Z"], [Assignment(7, "EM6")])], [])


def option(key, kind, side, after, params=None, unreachable=0, changes=()):
    return Option(key=key, kind=kind, face_group="G3", side=side, params=params or {},
                  before=Summary(2, 0), after=Summary(after, unreachable), tool_changes=list(changes),
                  changes_plan=True)


BUY = option("add_tool/G3/+Z/BC45-910480", "add_tool", "shop", 1, {"type": "back_chamfer", "name": "910480"},
             changes=[ToolChange(7, "EM6", "910480")])
FIVE = option("route_machine/5", "route_machine", "shop", 1, {"axes": 5})
REMOVE = option("remove_feature/G3", "remove_feature", "part", 1)
LONG = option("add_tool/G3/-Z/EM6-L30", "add_tool", "shop", 1, {"name": "EM6-L30", "diameter": 6.0, "max_ld": 5})


def route(result, key):
    return next(r for r in result.routes if r.key == key)


def test_the_current_plan_is_priced_from_its_setups():
    # 1 batch × 2 setups × 0.5 h × $60 = $60; 10 parts × (2 × 0.1 h load + 0.5 h cycle) × $60 = $420
    current = route(cost_options(FACTS, SHOP, [], plan=PLAN), "current")
    assert current.side == "current" and current.setups == 2
    assert current.costs[10] == (480.0, 480.0)
    assert current.one_time == (0.0, 0.0)


def test_buying_the_cutter_pays_once_for_the_tool_and_the_relieved_jaws():
    # setup $30 + load and cycle 10 × 0.6 h × $60 = $360 + back-chamfer pass 10 × 0.05 h × $60 = $30
    # + one-time: cutter $243.50 + jaw blanks $40 + 0.5 h × $60 to cut the reliefs = $313.50
    buy = route(cost_options(FACTS, SHOP, [BUY], plan=PLAN), BUY.key)
    assert buy.one_time == (313.5, 313.5)
    assert buy.costs[10] == (733.5, 733.5)


def test_a_bought_tool_is_spread_over_the_parts_it_serves_but_the_jaws_are_not():
    """Rayyan, 2026-10-29: spread the cutter over Parts A and B. Their stock differs, so each re-cuts its jaws."""
    shared = replace(SHOP, costs=replace(COSTS, scenarios=replace(COSTS.scenarios, tool_parts=2)))
    result = cost_options(FACTS, shared, [BUY, LONG], plan=PLAN)
    # cutter $243.50 / 2 + jaw blanks $40 + 0.5 h × $60 of reliefs = $191.75; + $420 of machining (as above)
    assert route(result, BUY.key).one_time == (191.75, 191.75) and route(result, BUY.key).costs[10] == (611.75, 611.75)
    assert route(result, LONG.key).one_time == (40.0, 40.0)  # any bought tool, not only the catalog cutter


def test_once_the_shop_owns_the_cutter_the_current_plan_pays_its_pass_and_its_own_jaws():
    """Part B (S8): the cutter came from Part A, so its price isn't charged again. Part B's stock differs, so
    its relieved jaws are its own (Codex, PR #20: they were free)."""
    from anville.geometry import HARVEY_910480

    owned = replace(SHOP, back_chamfer_tools=[HARVEY_910480])
    plan = SetupPlan(3, [Setup(["+Z"], [Assignment(1, "EM12"), Assignment(7, "910480")])], [])
    current = route(cost_options(FACTS, owned, [], plan=plan), "current")
    # jaws $40 + 0.5 h × $60 = $70; $30 setup + 10 × (0.1 + 0.5 + 0.05) h × $60 = $390 → $490
    assert current.setups == 1 and current.one_time == (70.0, 70.0) and current.costs[10] == (490.0, 490.0)
    assert current.payback is None  # nothing is bought on this route


def test_a_current_five_axis_plan_is_priced_at_the_outside_shop():
    """Codex, PR #20: the shop has no 5-axis mill, so a 5-axis plan is outsourced, like route_machine/5."""
    plan = SetupPlan(5, [Setup(["+Z", "-Z"], [Assignment(1, "EM12"), Assignment(7, "EM6")])], [])
    current = route(cost_options(FACTS, SHOP, [], plan=plan), "current")
    assert current.machine == "outsource" and current.costs[10] == (1075.0, 1075.0)


def test_removing_a_back_chamfered_face_stops_paying_its_pass():
    from anville.geometry import HARVEY_910480

    owned = replace(SHOP, back_chamfer_tools=[HARVEY_910480])
    plan = SetupPlan(3, [Setup(["+Z"], [Assignment(1, "EM12"), Assignment(7, "910480")])], [])
    gone = option("remove_feature/G3", "remove_feature", "part", 1)
    gone.removed_faces = [7]
    # $30 setup + 10 × (0.1 + 0.5) h × $60 = $360 → $390: no pass for a chamfer that isn't there
    assert route(cost_options(FACTS, owned, [gone], plan=plan), gone.key).costs[10] == (390.0, 390.0)


def test_a_tie_is_not_a_recommendation():
    """RFC 0002 §4: recommended only if strictly cheaper than every other route at every corner."""
    # The long-reach tool at $90 costs 390 + 90 = $480, the same as the current plan.
    costs = replace(COSTS, long_reach_price=fixed(90.0))
    (verdict,) = cost_options(FACTS, replace(SHOP, costs=costs), [LONG], plan=PLAN).verdicts
    assert verdict.recommended is None and verdict.flip["by"] == 0.0


def test_a_current_plan_with_unreachable_faces_is_marked_incomplete():
    plan = SetupPlan(3, PLAN.setups, [Unreachable(9, "no_tool", "-Z: needs Ø≤2")])
    assert route(cost_options(FACTS, SHOP, [], plan=plan), "current").complete is False


def test_five_axis_is_outsourced_at_the_outside_rate_plus_a_fee_per_batch():
    # 1 batch × (1 setup × 0.5 h × $150 + $100) = $175; 10 × (0.1 + 0.5) h × $150 = $900
    five = route(cost_options(FACTS, SHOP, [FIVE], plan=PLAN), FIVE.key)
    assert five.side == "outsource" and five.costs[10] == (1075.0, 1075.0)


def test_a_part_edit_costs_its_new_plan_and_says_the_designers_time_is_not_priced():
    # 1 setup × 0.5 h × $60 = $30; 10 × 0.6 h × $60 = $360
    remove = route(cost_options(FACTS, SHOP, [REMOVE], plan=PLAN), REMOVE.key)
    assert remove.costs[10] == (390.0, 390.0) and remove.one_time == (0.0, 0.0)
    assert any("designer" in n for n in remove.unpriced)


def test_a_long_reach_endmill_is_a_one_time_purchase():
    long = route(cost_options(FACTS, SHOP, [LONG], plan=PLAN), LONG.key)
    assert long.one_time == (80.0, 80.0) and long.costs[10] == (470.0, 470.0)


def test_batches_step_with_quantity():
    # q = 25 at batch size 10 is 3 batches: 3 × 2 × 0.5 × 60 = 180; 25 × 0.7 × 60 = 1050
    shop = replace(SHOP, costs=replace(COSTS, scenarios=replace(COSTS.scenarios, quantities=(25,))))
    assert route(cost_options(FACTS, shop, [], plan=PLAN), "current").costs[25] == (1230.0, 1230.0)


def test_with_every_input_pinned_the_cheapest_route_is_recommended():
    result = cost_options(FACTS, SHOP, [BUY, FIVE, LONG], plan=PLAN)
    (verdict,) = result.verdicts
    assert verdict.quantity == 10 and verdict.recommended == LONG.key and verdict.flip is None


def test_a_route_that_loses_at_one_corner_is_not_recommended_and_the_corner_is_shown():
    # q = 100 (10 batches). Current: 10 × 2 × 0.5 × 60 + 100 × 0.7 × 60 = $4,800.
    # Buy: 10 × 0.5 × 60 + 100 × 0.65 × 60 + $70 jaws + cutter = $4,270 + cutter.
    # Cutter $100-$600: buying wins at $100 ($4,370), loses at $600 ($4,870) by $70; at the $350 midpoint it leads.
    costs = replace(COSTS, tool_price={"910480": Input(100.0, 600.0, "$", "placeholder", "test")},
                    scenarios=replace(COSTS.scenarios, quantities=(100,)))
    (verdict,) = cost_options(FACTS, replace(SHOP, costs=costs), [BUY], plan=PLAN).verdicts
    assert verdict.recommended is None and verdict.leader == BUY.key
    assert verdict.flip == {"corner": {"tool_price.910480": 600.0}, "winner": "current", "by": 70.0}


def test_a_plan_that_leaves_faces_unreachable_is_never_recommended():
    broken = option("set_depth/G3/+Z/24", "set_depth", "part", 1, unreachable=1)
    result = cost_options(FACTS, SHOP, [broken], plan=PLAN)
    assert route(result, broken.key).complete is False
    assert result.verdicts[0].recommended == "current"


def test_a_purchase_reports_its_payback_in_parts():
    # Buying saves $30 per batch (one setup) and $3 per part (0.1 h load less, 0.05 h pass more), against $313.50.
    # q = 50 is 5 batches: 150 + 150 = 300 < 313.5. q = 51 is 6 batches: 180 + 153 = 333 ≥ 313.5.
    buy = route(cost_options(FACTS, SHOP, [BUY], plan=PLAN), BUY.key)
    assert buy.payback == (51, 51)


def test_payback_is_where_buying_stays_cheaper_not_where_it_first_is():
    """A pass slower than the load it saves: within a batch, savings fall; each new batch's saved setup lifts them.
    Savings = $30 × batches − $0.60 × q − $313.50. q = 121 (13 batches): 390 − 72.6 = 317.4, ahead; but
    q = 130: 390 − 78 = 312, behind again. From q = 131 (14 batches) on it stays ahead: at a batch's end, 24 × b ≥ 313.5."""
    costs = replace(COSTS, load_time=fixed(0.05, "hr"), back_chamfer_pass=fixed(0.06, "hr"))
    buy = route(cost_options(FACTS, replace(SHOP, costs=costs), [BUY], plan=PLAN), BUY.key)
    assert buy.payback == (131, 131) and buy.payback_mid == 131


def test_the_midpoint_payback_is_one_number_inside_the_corner_range():
    """For a slot like "pays off past N parts, at mid-range inputs": always shown with the range."""
    buy = route(cost_options(FACTS, Shop(tools=[], name="dll", costs=DLL_COSTS), [BUY], plan=PLAN), BUY.key)
    lo, hi = buy.payback
    assert lo <= buy.payback_mid and (hi is None or buy.payback_mid <= hi)
    assert route(cost_options(FACTS, SHOP, [BUY], plan=PLAN), BUY.key).payback_mid == 51
    assert route(cost_options(FACTS, SHOP, [], plan=PLAN), "current").payback_mid is None


def test_an_unknown_cutter_price_is_an_error_not_a_free_tool():
    other = option("add_tool/G3/+Z/BC45-999", "add_tool", "shop", 1, {"type": "back_chamfer", "name": "999"})
    with pytest.raises(ValueError, match="999"):
        cost_options(FACTS, SHOP, [other], plan=PLAN)


def test_an_option_kind_the_model_cannot_price_is_an_error():
    with pytest.raises(ValueError, match="add_fixture"):
        cost_options(FACTS, SHOP, [option("add_fixture/x", "add_fixture", "shop", 1)], plan=PLAN)


def test_a_shop_without_costs_is_an_error():
    from anville.geometry import ShopProfile

    with pytest.raises(ValueError, match="cost"):
        cost_options(FACTS, ShopProfile(tools=[]), [], plan=PLAN)


# ---- the corner bounds are exact ----------------------------------------------------------

def test_no_point_inside_the_box_escapes_the_corner_bounds():
    """Re-derive the multiaffine claim: random interior points never fall outside the corner min and max."""
    result = cost_options(FACTS, Shop(tools=[], name="dll", costs=DLL_COSTS), [BUY, FIVE, REMOVE], plan=PLAN)
    from anville.shop.cost import _route_cost, _ranged

    rng = random.Random(7)
    ranged = _ranged(DLL_COSTS)
    for r in result.routes:
        for q, (lo, hi) in r.costs.items():
            for _ in range(300):
                point = {name: rng.uniform(i.lo, i.hi) for name, i in ranged.items()}
                v = _route_cost(r, point, q, DLL_COSTS)
                assert lo - 1e-6 <= v <= hi + 1e-6


def test_payback_bounds_hold_inside_the_box():
    shop = Shop(tools=[], name="dll", costs=DLL_COSTS)
    result = cost_options(FACTS, shop, [BUY], plan=PLAN)
    from anville.shop.cost import _payback_at, _ranged

    lo, hi = route(result, BUY.key).payback
    rng = random.Random(11)
    ranged = _ranged(DLL_COSTS)
    for _ in range(300):
        point = {name: rng.uniform(i.lo, i.hi) for name, i in ranged.items()}
        p = _payback_at(route(result, BUY.key), route(result, "current"), point, DLL_COSTS)
        if p is None:
            assert hi is None  # never pays back inside the box, so some corner says so too
        else:
            assert lo <= p and (hi is None or p <= hi)


# ---- the demo profile labels what it doesn't know ------------------------------------------

def test_only_the_cutter_price_is_verified_and_only_the_mill_rate_is_sourced():
    inputs = cost_options(FACTS, Shop(tools=[], name="dll", costs=DLL_COSTS), [], plan=PLAN).inputs
    status = {name: i["status"] for name, i in inputs.items()}
    assert status.pop("tool_price.910480") == "verified" and inputs["tool_price.910480"]["lo"] == 243.5
    assert status.pop("mill_rate") == "sourced" and (inputs["mill_rate"]["lo"], inputs["mill_rate"]["hi"]) == (40, 120)
    assert set(status.values()) == {"placeholder"}
    assert all(i["source"] for i in inputs.values())


@pytest.mark.parametrize("bad", [dict(lo=2, hi=1), dict(status="guessed")])
def test_an_input_must_be_a_range_with_a_known_status(bad):
    with pytest.raises(ValueError):
        Input(**{**dict(lo=1, hi=2, unit="$", status="placeholder", source="x"), **bad})


def test_corners_cover_every_ranged_input():
    from anville.shop.cost import _corners, _ranged

    ranged = _ranged(DLL_COSTS)
    corners = list(_corners(ranged))
    assert len(corners) == 2 ** len(ranged)
    for name, i in ranged.items():
        assert {c[name] for c in corners} == {i.lo, i.hi}
    assert len({tuple(sorted(c.items())) for c in corners}) == len(corners)
    assert all(i.lo < i.hi for i in ranged.values())  # pinned inputs add no corners


def test_payback_never_reached_still_runs_fast():
    """Greptile, PR #20: a purchase that never pays back scanned every quantity at every corner (9 s measured)."""
    import time

    never = replace(DLL_COSTS, back_chamfer_pass=Input(1.0, 2.0, "hr", "placeholder", "worst case"))
    start = time.perf_counter()
    buy = route(cost_options(FACTS, Shop(tools=[], name="dll", costs=never), [BUY], plan=PLAN), BUY.key)
    assert buy.payback == (None, None) and buy.payback_mid is None  # never, at any corner
    assert time.perf_counter() - start < 2.0


def test_payback_holds_past_any_scan_limit():
    """Codex, PR #20: with 750-part batches, buying led from part 751 but was $193.25 dearer at 1,500.
    The gap grows by d_batch + n × d_part each batch; when that is positive, buying never stays cheaper."""
    costs = replace(COSTS, setup_time=fixed(5.0, "hr"), back_chamfer_pass=fixed(0.1066833333, "hr"),
                    scenarios=replace(COSTS.scenarios, batch_size=750, tool_parts=2))
    buy = route(cost_options(FACTS, replace(SHOP, costs=costs), [BUY], plan=PLAN), BUY.key)
    assert buy.payback == (None, None)


def test_payback_far_past_a_thousand_parts_is_found_exactly():
    """The old scan stopped at 1,000 parts and called this "never". The pass (0.149 h) costs $2.94 a part more
    than the load it saves (0.1 h); each batch's saved setup wins back $30, so each batch nets $0.60. Within a
    batch buying loses ground, so its worst point is the batch's last part: buy − current = $313.50 − $0.60k.
    At q = 5,220 (batch 522) that is +$0.30; at q = 5,230 it is −$0.30. Buying stays cheaper from q = 5,221."""
    costs = replace(COSTS, back_chamfer_pass=fixed(0.149, "hr"))
    buy = route(cost_options(FACTS, replace(SHOP, costs=costs), [BUY], plan=PLAN), BUY.key)
    assert buy.payback == (5221, 5221)


def test_payback_when_buying_adds_a_setup_but_saves_per_part():
    """A purchase that adds a setup per batch but saves per part peaks at each batch's first part.
    Current: 1 setup, back-chamfered with a 0.2 h pass, $70 of jaws. Buy: an $80 long-reach endmill, 2 setups, no
    pass. buy − current = $10 + $30 × batches − $6 × q: q = 11 is +$4 (a new batch's setup), q = 12 is −$2."""
    from anville.shop.cost import Route, _payback_at, _route_cost

    costs = replace(COSTS, back_chamfer_pass=fixed(0.2, "hr"))
    current = Route("current", "current", 1, True, "mill", ["jaw_blank", "jaw_machining"], 1)
    buy = Route("add_tool/x", "shop", 2, True, "mill", ["long_reach_price"], 0)
    assert _payback_at(buy, current, {}, costs) == 12
    last_loss = max(q for q in range(1, 500) if _route_cost(buy, {}, q, costs) > _route_cost(current, {}, q, costs) + 1e-9)
    assert last_loss + 1 == 12  # the same answer by brute force
