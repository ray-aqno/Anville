"""The Part B learning beat, end to end: Part A's review buys the 910480, the ledger records it, and Part B is
planned, priced and sheeted with the cutter the shop now owns, saying where it came from and when."""

from datetime import datetime, timezone

from anville.geometry import assess_back_chamfer, evaluate_edits, min_setups
from anville.store import Store
from anville.shop import apply_ledger, cost_options, setup_sheet, tool_acquired
from tests.shop.test_demo_costs import DLL, _part_a

# 02:30 UTC on the 30th is still the 29th in Chicago: a local-time date would be wrong (Codex, Part B review)
NOTED = datetime(2026, 10, 30, 2, 30, tzinfo=timezone.utc).timestamp()


def _part_b_after_part_a(step, facts, tmp_path):
    """Part A's purchase goes through the real store, as the job does it, and comes back for Part B."""
    _, options, _ = _part_a(step, facts)
    (buy,) = [o for o in options if o.key.endswith("BC45-910480")]
    entry = tool_acquired(buy, review="review:part-a/v3", decision="comment:c42")
    store = Store(tmp_path / "anville.db", clock=lambda: NOTED)
    store.append_ledger("shop_outcome", "shop:dll", entry["payload"], entry["provenance"])
    shop, provenance = apply_ledger(DLL, store.read_ledger("shop_outcome", "shop:dll"))
    b = facts("demo_bracket_b")
    access = assess_back_chamfer(step("demo_bracket_b"), b, shop)
    return b, shop, provenance, access, min_setups(b, shop, back_chamfer=access)


def test_part_b_is_one_setup_and_buys_nothing_once_part_a_bought_the_cutter(step, facts, tmp_path):
    b, shop, _, access, plan = _part_b_after_part_a(step, facts, tmp_path)
    assert type(shop) is type(DLL) and shop.costs is DLL.costs  # still the priced shop
    face = b.edge_chamfers[0].face_id
    group = next(label for label, ids in b.labelled_groups().items() if face in ids)
    options = [o for o in evaluate_edits(b, shop, [{"kind": "add_tool", "face_group": group}],
                                         back_chamfer=access).options if o.changes_plan]
    result = cost_options(b, shop, options, plan=plan)
    current = next(r for r in result.routes if r.key == "current")
    assert plan.count == 1 and current.setups == 1 and current.chamfers == 1
    assert not any(n.startswith("tool_price.") for r in result.routes for n in r.buys)  # the cutter isn't bought again
    assert current.buys == ["jaw_blank", "jaw_machining"] and current.payback is None  # Part B's own jaws only


def test_part_b_sheet_says_where_the_cutter_came_from_and_when(step, facts, tmp_path):
    b, shop, provenance, access, plan = _part_b_after_part_a(step, facts, tmp_path)
    (only,) = setup_sheet(b, shop, plan, back_chamfer=access, provenance=provenance)["setups"]
    (cutter,) = [t for t in only["tools"] if t["type"] == "back_chamfer"]
    assert cutter["acquired"]["on"] == "2026-10-30"
    assert cutter["acquired"]["review"] == "review:part-a/v3" and cutter["acquired"]["decision"] == "comment:c42"
    assert cutter["acquired"]["why"] == "bought on 2026-10-30, after review review:part-a/v3 (comment:c42)"


def test_with_nothing_in_the_ledger_part_b_still_flips(step, facts, tmp_path):
    """The beat comes from the stored purchase: an empty ledger leaves Part B at 2 setups."""
    store = Store(tmp_path / "anville.db", clock=lambda: NOTED)
    shop, provenance = apply_ledger(DLL, store.read_ledger("shop_outcome", "shop:dll"))
    b = facts("demo_bracket_b")
    access = assess_back_chamfer(step("demo_bracket_b"), b, shop)
    assert provenance == {} and min_setups(b, shop, back_chamfer=access).count == 2
