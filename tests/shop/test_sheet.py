"""The SetupSheet (shop -> panel -> main): what the shop runs a plan from, as JSON."""

import json
from dataclasses import replace

import pytest

from anville.geometry import HARVEY_910480, ShopProfile, SoftJaws, Tool, assess_back_chamfer, min_setups
from anville.geometry.plan import Assignment, Setup, SetupPlan, Unreachable
from anville.shop.sheet import SCHEMA, setup_sheet

SHOP = ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)], fixture=SoftJaws())
OWNED = replace(SHOP, back_chamfer_tools=[HARVEY_910480])


def _one_setup(step, facts, name):
    f = facts(name)
    access = assess_back_chamfer(step(name), f, OWNED)
    return f, min_setups(f, OWNED, back_chamfer=access), access


def test_the_flip_plan_is_two_setups_and_the_second_is_bottom_up(facts):
    f = facts("demo_bracket")
    sheet = setup_sheet(f, SHOP, min_setups(f, SHOP))
    assert sheet["schema"] == SCHEMA and [s["number"] for s in sheet["setups"]] == [1, 2]
    assert [s["directions"] for s in sheet["setups"]] == [["+Z"], ["-Z"]]
    assert "bottom up" in sheet["setups"][1]["orientation"]
    assert all(not s["workholding"]["reliefs"] and not s["notes"] for s in sheet["setups"])
    json.dumps(sheet)  # the panel and the comment link read it as JSON


def test_each_setup_lists_its_tools_with_sizes_and_the_faces_they_cut(facts):
    f = facts("demo_bracket")
    plan = min_setups(f, SHOP)
    (first, _) = setup_sheet(f, SHOP, plan)["setups"]
    by_name = {t["name"]: t for t in first["tools"]}
    assert by_name["EM12"] == {**by_name["EM12"], "type": "endmill", "diameter_mm": 12.0, "reach_mm": 48.0}
    cut = {a.face_id for a in plan.setups[0].faces}
    assert {fid for t in first["tools"] for fid in t["faces"]} == cut
    labels = {fid: g for g, ids in f.labelled_groups().items() for fid in ids}
    assert all(t["groups"] == sorted({labels[fid] for fid in t["faces"]}, key=lambda g: int(g[1:]))
               for t in first["tools"])


@pytest.mark.parametrize("name, grip", [("demo_bracket", "X"), ("demo_bracket_b", "Y")])
def test_with_the_cutter_one_setup_holds_the_part_in_relieved_jaws(step, facts, name, grip):
    f, plan, access = _one_setup(step, facts, name)
    (only,) = setup_sheet(f, OWNED, plan, back_chamfer=access)["setups"]
    held = only["workholding"]
    assert held["type"] == "vise_soft_jaws" and held["relieved"] is True
    assert held["grip_faces"] == [f"-{grip}", f"+{grip}"]
    assert sorted(r["jaw"] for r in held["reliefs"]) == [f"+{grip} jaw", f"-{grip} jaw"]
    i, (lo, hi) = "XYZ".index(grip), f.bbox
    for r in held["reliefs"]:  # the -X jaw's pocket lies past the part's X minimum, the +X jaw's past its maximum
        assert r["origin_mm"][i] < lo[i] if r["jaw"][0] == "-" else r["origin_mm"][i] >= hi[i] - 1e-6
    assert held["parallels"]["setback_mm"] == pytest.approx(HARVEY_910480.head_diameter / 2 + 1.0)
    (cutter,) = [t for t in only["tools"] if t["type"] == "back_chamfer"]
    assert cutter["name"] == "910480" and cutter["head_diameter_mm"] == 12.7 and cutter["neck_diameter_mm"] == 8.05
    assert cutter["faces"] == [f.edge_chamfers[0].face_id]
    assert any("verify the back-chamfer toolpath" in n for n in only["notes"])


def test_part_a_relief_is_the_one_rfc_0002_derived(step, facts):
    """RFC 0002 §3: a 13.7 × 14.7 mm relief at the chamfer-side corner, full jaw height; each jaw keeps 96%."""
    f, plan, access = _one_setup(step, facts, "demo_bracket")
    reliefs = setup_sheet(f, OWNED, plan, back_chamfer=access)["setups"][0]["workholding"]["reliefs"]
    assert {(r["size_mm"][0], r["size_mm"][1]) for r in reliefs} == {(13.7, 14.7)}
    assert {r["grip_kept"] for r in reliefs} == {0.958}


def test_the_workholding_says_it_is_stated_not_confirmed(facts):
    f = facts("demo_bracket")
    held = setup_sheet(f, SHOP, min_setups(f, SHOP))["setups"][0]["workholding"]
    assert "not confirmed" in held["status"]


def test_a_cutter_from_the_ledger_says_where_it_came_from(step, facts):
    f, plan, access = _one_setup(step, facts, "demo_bracket_b")
    source = {"review": "review:part-a/v3", "decision": "comment:c42", "option": "add_tool/G2/+Z/BC45-910480"}
    sheet = setup_sheet(f, OWNED, plan, back_chamfer=access, provenance={"910480": source})
    (cutter,) = [t for t in sheet["setups"][0]["tools"] if t["type"] == "back_chamfer"]
    assert cutter["acquired"] == source


def test_unreachable_faces_are_on_the_sheet_so_the_shop_sees_what_the_plan_skips(facts):
    f = facts("demo_bracket")
    plan = min_setups(f, SHOP)
    face = plan.setups[0].faces[0].face_id
    broken = SetupPlan(3, plan.setups, [Unreachable(face, "no_tool", "+Z: needs Ø≤2 reaching 30 mm")])
    (row,) = setup_sheet(f, SHOP, broken)["unreachable"]
    assert row["face_id"] == face and row["reason"] == "no_tool" and row["group"].startswith("G")


def test_a_back_chamfer_plan_without_its_assessments_is_refused(step, facts):
    """Greptile, PR #20: the sheet would say "cut the jaw reliefs" and list none."""
    f, plan, _ = _one_setup(step, facts, "demo_bracket")
    with pytest.raises(ValueError, match="910480"):
        setup_sheet(f, OWNED, plan)


def test_a_back_chamfer_plan_whose_assessment_failed_is_refused(step, facts):
    f, plan, access = _one_setup(step, facts, "demo_bracket")
    failed = [replace(a, checks=[*a.checks, replace(a.checks[0], name="f", passed=False)]) for a in access]
    with pytest.raises(ValueError, match="910480"):
        setup_sheet(f, OWNED, plan, back_chamfer=failed)


def test_a_plan_with_a_tool_the_shop_does_not_have_is_an_error(facts):
    f = facts("demo_bracket")
    plan = SetupPlan(3, [Setup(["+Z"], [Assignment(f.faces[0].id, "EM99")])], [])
    with pytest.raises(ValueError, match="EM99"):
        setup_sheet(f, SHOP, plan)


def test_stock_size_and_the_part_it_is_for(facts):
    f = facts("demo_bracket")
    sheet = setup_sheet(f, SHOP, min_setups(f, SHOP))
    assert sheet["part"] == {"source_sha256": f.source_sha256, "stock_mm": [100.0, 60.0, 20.0]}
    assert sheet["basis"] == "predicted"
