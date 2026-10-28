"""The demo's fix branch (handoff 2026-10-28): suppressing a bottom chamfer drops a setup, predicted and measured."""

import pytest

from anville.geometry import evaluate_edits, min_setups

DEMO = ["demo_bracket", "demo_bracket_b"]


def _bottom_chamfer(f):
    (face,) = [x for x in f.faces if x.normal and -0.75 < x.normal[2] < -0.65]
    return face


def _label(f, face_id):
    return next(label for label, ids in f.labelled_groups().items() if face_id in ids)


@pytest.mark.parametrize("name", DEMO)
def test_bottom_chamfer_is_the_only_face_needing_minus_z(name, facts, shop):
    f = facts(name)
    plan = min_setups(f, shop)
    assert plan.unreachable == []
    assert [a.face_id for a in plan.setups[1].faces] == [_bottom_chamfer(f).id]


def test_top_edge_break_comes_free_in_the_top_setup(facts, shop):
    f = facts("demo_bracket")
    (top_chamfer,) = [x for x in f.faces if x.normal and 0.65 < x.normal[2] < 0.75]
    assert top_chamfer.id in {a.face_id for a in min_setups(f, shop).setups[0].faces}


@pytest.mark.parametrize("name", DEMO)
def test_removing_the_chamfer_predicts_what_suppressing_it_measures(name, facts, shop):
    f = facts(name)
    chamfer = _bottom_chamfer(f)
    (option,) = evaluate_edits(f, shop, [{"kind": "remove_feature", "face_group": _label(f, chamfer.id)}]).options
    assert option.side == "part" and option.removed_faces == [chamfer.id] and option.changes_plan
    assert (option.before.setups, option.after.setups) == (2, 1)
    measured = min_setups(facts(f"{name}_unchamfered"), shop)
    assert option.after.setups == measured.count and option.after.unreachable == len(measured.unreachable)


def test_five_axis_reaches_the_chamfer_without_a_flip(facts, shop):
    assert min_setups(facts("demo_bracket"), shop, axes=5).count == 1
