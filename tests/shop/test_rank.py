"""rank_fixes (ADR 0010): which any-intent fixes code puts in the top tier, and which one leads."""

from dataclasses import replace

import pytest

from anville.shop import Input, Ranking, cost_options, rank_fixes
from tests.shop.test_cost import BUY, COSTS, FACTS, FIVE, PLAN, SHOP, fixed, option
from tests.shop.test_demo_costs import DLL, _part_a


def _rank(shop, options, keys=None, **kw):
    return rank_fixes(cost_options(FACTS, shop, options, plan=PLAN), shop,
                      keys or [o.key for o in options], **kw)


def test_a_fix_dearer_at_every_corner_is_out_of_the_top_tier():
    # Pinned inputs, q = 10: buying $733.50, routing out $1,075 (test_cost derives both)
    assert _rank(SHOP, [BUY, FIVE], [FIVE.key, BUY.key]) == Ranking([BUY.key], BUY.key, "dominates")


def test_a_cost_left_unpriced_keeps_a_cheaper_fix_from_outranking_or_leading():
    """Routing out at $10/hr is cheaper in dollars, but its lead time isn't priced and buying has no such gap."""
    cheap = replace(SHOP, costs=replace(COSTS, outsource_rate=fixed(10.0, "$/hr"), outsource_fee=fixed(0.0)))
    assert _rank(cheap, [BUY, FIVE]) == Ranking([BUY.key, FIVE.key], None, "tie")


def test_without_dominance_the_cheapest_at_mid_range_inputs_leads_the_tie():
    """Rayyan, 2026-10-30: the tie-break amendment. The tier stays a tie; the mid-range leader goes first."""
    ranged = replace(SHOP, costs=replace(COSTS, outsource_rate=Input(40.0, 200.0, "$/hr", "placeholder", "test")))
    # At $40/hr routing out costs less than buying; at $200/hr far more. Midpoint $120/hr: buying is cheaper.
    assert _rank(ranged, [FIVE, BUY]) == Ranking([FIVE.key, BUY.key], BUY.key, "mid_range")


def test_the_mid_range_leader_must_lead_at_every_scenario_quantity():
    """Two parts: the cutter's one-time cost makes buying dearer ($421.50 vs $355). A hundred: far cheaper
    ($4,513.50 vs $10,750). No fix leads at both, so the tie stays open and the model chooses."""
    two = replace(SHOP, costs=replace(COSTS, scenarios=replace(COSTS.scenarios, quantities=(2, 100))))
    assert _rank(two, [BUY, FIVE]) == Ranking([BUY.key, FIVE.key], None, "tie")


def test_a_stated_quantity_replaces_the_scenarios():
    two = replace(SHOP, costs=replace(COSTS, scenarios=replace(COSTS.scenarios, quantities=(2, 100))))
    assert _rank(two, [BUY, FIVE], quantity=100) == Ranking([BUY.key], BUY.key, "dominates")


def test_equal_costs_are_a_tie_with_no_leader():
    twin = replace(BUY, key="add_tool/G3/+Z/BC45-910480-twin")
    assert _rank(SHOP, [BUY, twin]) == Ranking([BUY.key, twin.key], None, "tie")


def test_a_fix_that_leaves_faces_unreachable_is_never_in_the_top_tier():
    broken = option("add_tool/G3/+Z/cheap", "add_tool", "shop", 1, {"name": "x", "diameter": 6.0, "max_ld": 5},
                    unreachable=1)
    assert _rank(SHOP, [broken, FIVE]) == Ranking([FIVE.key], FIVE.key, "sole")
    assert _rank(SHOP, [broken]) == Ranking([], None, "none")


def test_a_key_that_wasnt_priced_is_an_error():
    with pytest.raises(ValueError, match="nope"):
        _rank(SHOP, [BUY], [BUY.key, "nope"])


def test_part_a_ties_buy_and_route_and_the_910480_leads_on_mid_range_cost(step, facts):
    """The demo (orchestrator, 2026-10-30): with intent stated, the model led with routing out 10 of 10 times.
    On the DLL's ranges neither fix is cheaper at every corner, at 4, 10 or 20 parts, so both stay in the
    top tier; at mid-range inputs buying is cheaper at all three ($496/$788/$1,355 vs $549/$1,017/$2,033)."""
    f, options, plan = _part_a(step, facts)
    fixes = [o for o in options if o.side == "shop"]  # any-intent: changes the shop, not the part
    keys = [o.key for o in fixes]
    (buy,) = [k for k in keys if k.endswith("BC45-910480")]
    ranking = rank_fixes(cost_options(f, DLL, fixes, plan=plan), DLL, keys)
    assert set(ranking.top) == {buy, "route_machine/5"} and ranking.top == keys
    assert ranking.lead == buy and ranking.basis == "mid_range"



def test_dominance_must_hold_at_every_scenario_quantity():
    """At 100 parts buying is far cheaper; at 2 it's dearer. Listed 100 first, it still doesn't outrank."""
    both = replace(SHOP, costs=replace(COSTS, scenarios=replace(COSTS.scenarios, quantities=(100, 2))))
    assert _rank(both, [BUY, FIVE]) == Ranking([BUY.key, FIVE.key], None, "tie")


def test_ranking_needs_a_quantity_to_compare_at():
    """Codex, ADR 0010 review: with no quantities every comparison held vacuously and the tier came back empty."""
    none = replace(SHOP, costs=replace(COSTS, scenarios=replace(COSTS.scenarios, quantities=())))
    with pytest.raises(ValueError, match="quantity"):
        _rank(none, [BUY, FIVE])


def test_a_waived_cost_no_longer_keeps_the_removal_from_leading(step, facts):
    """Rayyan, 2026-10-31: once the designer says the chamfer may change, the free removal is ranked with the
    shop fixes. Its only unpriced cost is the designer's own time to make the edit, which they volunteered, so
    it's waived; without the waiver, nothing that leaves a cost unpriced may outrank a fix that doesn't."""
    from anville.shop.cost import DESIGNER_TIME

    f, options, plan = _part_a(step, facts)
    keys = [o.key for o in options]
    (removal,) = [k for k in keys if k.startswith("remove_feature/")]
    result = cost_options(f, DLL, options, plan=plan)
    assert rank_fixes(result, DLL, keys).lead != removal
    # Cheaper than buying at every corner; routing out costs less at some extremes, so the removal leads on
    # mid-range cost, and the comment names the route as its rival.
    assert rank_fixes(result, DLL, keys, waived={DESIGNER_TIME}) == Ranking(
        [removal, "route_machine/5"], removal, "mid_range")
