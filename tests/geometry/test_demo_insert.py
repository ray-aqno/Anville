"""Part B for the recording: the suspension tube insert (docs/demo/build-sheet.md), on the live review's shop.

The learning beat needs exactly this: 2 setups on a fresh shop, with the bottom chamfer as the only issue; 1 setup
once the shop owns the 910480 that Part A's review offered; checks a-f passing; and plain-words phrases for the
chamfer and the bore. The shop is `review_shop`, the one production's `analyze` plans with.
"""

from dataclasses import replace

import pytest

from anville.agent.job import catalog_for, removal_alternative, review_shop, sheet_for, shop_note_writes
from anville.agent.review import find_issues
from anville.geometry import assess_back_chamfer, describe_group, evaluate_edits, min_setups

NAME = "demo_insert"


def _review(step, f, ledger):
    """Production's analyze: checks a-f for the owned cutters and the catalog's, then the plan."""
    shop, _ = review_shop(ledger)
    access = assess_back_chamfer(step, f, replace(shop, back_chamfer_tools=[*shop.back_chamfer_tools, *catalog_for(shop)]))
    return shop, access, min_setups(f, shop, back_chamfer=access)


def _chamfer_group(f):
    (face,) = [x for x in f.faces if x.normal and -0.75 < x.normal[2] < -0.65]
    return next(g for g, ids in f.labelled_groups().items() if face.id in ids)


@pytest.fixture(scope="module")
def fresh(step, facts):
    f = facts(NAME)
    return (f, *_review(step(NAME), f, []))


def test_a_fresh_shop_flips_it_for_the_chamfer_alone(fresh):
    f, _, _, plan = fresh
    assert [s.directions for s in plan.setups] == [["+Z"], ["-Z"]] and plan.unreachable == []
    issues = find_issues(f, plan)
    assert [(i.kind, i.face_group) for i in issues] == [("forces_setup", _chamfer_group(f))]


def test_checks_a_to_f_pass_for_the_910480(fresh):
    _, _, access, _ = fresh
    (row,) = access
    assert row.tool == "910480" and [c.name for c in row.checks] == list("abcdef")
    assert all(c.passed for c in row.checks), [(c.name, c.detail) for c in row.checks if not c.passed]


def test_once_part_a_s_cutter_arrives_it_is_one_setup(step, facts, fresh):
    """The learning beat, the way the live app writes it (modal_app._on_shop_note): Part A's setup sheet plans
    with the 910480 it decided to buy, the machinist ticks it as arrived, and Part B is planned from that ledger.
    A designer's reply alone writes no acquisition: before the note, Part B is still 2 setups."""
    f, shop, _, _ = fresh
    a = facts("demo_bracket")
    _, a_access, _ = _review(step("demo_bracket"), a, [])
    sheet = sheet_for(a.to_dict(), shop, [x.to_dict() for x in a_access], buying=["910480"])
    note = {"note_id": "n1", "sheet_id": "s1", "outcome": "as_planned", "tools_arrived": ["910480"], "tags": [],
            "setups_run": 1, "note_text": ""}
    ledger = shop_note_writes(note, sheet, stored=[])
    assert [e["payload"]["kind"] for e in ledger] == ["shop_note", "tool_acquired"]
    owned, _, plan = _review(step(NAME), f, ledger)
    assert [t.name for t in owned.back_chamfer_tools] == ["910480"]
    assert plan.count == 1 and plan.unreachable == [] and find_issues(f, plan) == []


def test_its_options_are_part_a_s_and_removal_measures_what_it_predicts(fresh, facts):
    f, shop, access, _ = fresh
    g = _chamfer_group(f)
    opts = evaluate_edits(f, shop, [{"kind": k, "face_group": g} for k in ("remove_feature", "add_tool")],
                          back_chamfer=access).options
    after = {o.key: (o.after.setups, o.after.unreachable) for o in opts}
    assert after == {f"remove_feature/{g}": (1, 0), f"add_tool/{g}/+Z/BC45-910480": (1, 0),
                     f"add_tool/{g}/+X/EM12-L72": (2, 0), f"add_tool/{g}/-X/EM12-L72": (2, 0)}
    alt = removal_alternative(f.to_dict(), shop, [a.to_dict() for a in access])
    assert alt.face_group == g and alt.after.setups == min_setups(facts(f"{NAME}_unchamfered"), shop).count == 1


def test_the_beat_s_faces_read_in_plain_words(fresh):
    f, *_ = fresh
    got = {describe_group(f, g) for g in f.labelled_groups()}
    assert describe_group(f, _chamfer_group(f)) == "the 1 mm chamfer on the bottom back edge"
    assert "the Ø20 through hole" in got


def test_the_twin_is_the_build_sheet_s_solid(step, facts):
    """The recipe's sizes, so the twin can't drift from what Rayyan models: the volume Onshape's Mass properties
    should show (33,899.6 mm³, also by hand), the outline, and each round feature's size and place."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    reader.ReadFile(str(step(NAME)))
    reader.TransferRoots()
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(reader.OneShape(), props)
    assert props.Mass() == pytest.approx(33899.6, abs=0.1)

    f = facts(NAME)
    assert f.bbox == ((-35, -20, 0), (35, 20, 22))
    round_ = sorted((round(x.radius, 3), x.concave_radius is not None, round(x.centroid[0], 3)) for x in f.faces
                    if x.surface == "cylinder")
    assert round_ == [(3.25, True, -25.0), (3.25, True, 25.0), (10.0, True, 0.0), (16.0, False, 0.0)]
