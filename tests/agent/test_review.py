"""The review loop (ADR 0009) on demo Part A, with a scripted model.

Part A: 2 setups (+Z, -Z), and the bottom weld-clearance chamfer is the only face in -Z.
evaluate_edits offers remove_feature (part side, 2 -> 1) and route_machine/5 (shop side, 2 -> 1).
"""

import json

import pytest

from anville.agent.model import ModelUnavailable, Reply, parse_reply
from anville.agent.review import (
    Intent, ask_allowed, fill_slots, filter_requests, find_issues, run_review, tag_intents,
)
from anville.geometry import ShopProfile, Tool, extract, min_setups


@pytest.fixture(scope="module")
def shop():
    return ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)])


@pytest.fixture(scope="module")
def part_a(tmp_path_factory):
    from tests.geometry.parts import PARTS, write_step  # OCP

    return extract(write_step(PARTS["demo_bracket"](), tmp_path_factory.mktemp("agent") / "a.step"))


@pytest.fixture(scope="module")
def chamfer(part_a, shop):
    (issue,) = find_issues(part_a, min_setups(part_a, shop))
    return issue.face_group


class Scripted:
    """A model that returns prepared tool calls in order, and records what it was sent."""

    name = "scripted"

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def call(self, messages, tools, *, max_tokens=None):
        self.sent.append((messages, tools, max_tokens))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def call(tool, **args):
    return Reply(tool=tool, arguments=args, finish_reason="tool_calls")


def happy_path(chamfer, decision):
    return Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "it alone forces a flip"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "remove_feature", "face_group": chamfer},
                                         {"kind": "route_machine"}]),
        call("decide", decisions=[decision]),
        call("write_comment", text=f"{chamfer} forces setup {{{{issue.I1.directions}}}}. Routing it to 5-axis takes "
                                   "the part from {{option.route_machine/5.setups_before}} to "
                                   "{{option.route_machine/5.setups_after}} setups."),
    )


# --- code stages ------------------------------------------------------------------


def test_the_bottom_chamfer_is_the_one_computed_issue(part_a, shop):
    plan = min_setups(part_a, shop)
    issues = find_issues(part_a, plan)
    assert plan.count == 2
    assert [(i.kind, i.detail["directions"]) for i in issues] == [("forces_setup", ["-Z"])]


def test_a_ledger_constraint_outranks_a_stated_reason(part_a, shop, chamfer):
    issues = find_issues(part_a, min_setups(part_a, shop))
    stated = {chamfer: "Weld clearance chamfer"}
    assert tag_intents(issues, stated)[chamfer].status == "protected"
    may = [{"id": 7, "face_group": chamfer, "rule": "may_change", "reason": "this bracket bolts to a plate"}]
    assert tag_intents(issues, stated, may)[chamfer] == Intent("free", "this bracket bolts to a plate", "ledger:7")


def test_part_edits_on_a_protected_face_never_reach_the_planner(chamfer):
    intents = {chamfer: Intent("protected", "Weld clearance chamfer", "cad_feature_name")}
    kept, rejected = filter_requests([{"kind": "remove_feature", "face_group": chamfer},
                                      {"kind": "add_tool", "face_group": chamfer},
                                      {"kind": "route_machine"}], intents)
    assert [r["kind"] for r in kept] == ["add_tool", "route_machine"]
    assert rejected == [{"kind": "remove_feature", "face_group": chamfer,
                         "reason": "protected: Weld clearance chamfer"}]


def test_ask_is_allowed_only_when_the_answer_changes_the_plan(part_a, shop, chamfer):
    (issue,) = find_issues(part_a, min_setups(part_a, shop))
    # Unknown intent: removing the chamfer (part side) beats routing to 5-axis, so the answer matters.
    assert ask_allowed(part_a, shop, issue, Intent("unknown")) is True
    # Stated intent: nothing to ask.
    assert ask_allowed(part_a, shop, issue, Intent("protected", "weld clearance")) is False


def test_slots_fill_numbers_and_reject_numbers_the_model_wrote():
    values = {"plan.setups": "2", "option.route_machine/5.setups_after": "1"}
    keys = ["route_machine/5"]
    text, err = fill_slots("G3 forces {{plan.setups}} setups; route_machine/5 gives "
                           "{{option.route_machine/5.setups_after}} on a 5-axis, from -Z.", values, keys)
    assert (text, err) == ("G3 forces 2 setups; route_machine/5 gives 1 on a 5-axis, from -Z.", "")
    assert fill_slots("Saves 37 minutes per part.", values, keys)[0] is None
    assert "unknown slot" in fill_slots("{{option.made_up.setups_after}}", values, keys)[1]


# --- the loop -------------------------------------------------------------------


def test_stated_intent_review_proposes_the_shop_side_fix_in_four_calls(part_a, shop, chamfer):
    model = happy_path(chamfer, {"issue_id": "I1", "action": "propose", "option_key": "route_machine/5",
                                 "reason": "the chamfer clears a weld; keep it and change the shop"})
    events = []
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"},
                   emit=lambda kind, title, **d: events.append((kind, title, d)))

    assert r.model_calls == 4 and r.fallbacks == []
    assert [x["kind"] for x in r.rejected] == ["remove_feature"]  # filtered before scoring
    assert list(r.options) == ["route_machine/5"]
    assert r.ask_allowed == {"I1": False}
    assert r.comment.endswith("takes the part from 2 to 1 setups.")
    assert {d["stage"] for _, _, d in events} >= {"facts", "triage", "intent", "options", "decide"}
    assert all(not any(c.isdigit() for c in title) for _, title, _ in events)  # ADR 0007: no numbers in titles


def test_proposing_an_option_that_was_not_offered_is_rejected_then_falls_back(part_a, shop, chamfer):
    bad = call("decide", decisions=[{"issue_id": "I1", "action": "propose",
                                     "option_key": f"remove_feature/{chamfer}", "reason": "fewest setups"}])
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "remove_feature", "face_group": chamfer},
                                         {"kind": "route_machine"}]),
        bad, bad,
        call("write_comment", text="Routing to 5-axis gives {{option.route_machine/5.setups_after}} setup."),
    )
    events = []
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"},
                   emit=lambda kind, title, **d: events.append((kind, d)))
    assert r.fallbacks == ["decide"]
    assert [(d.action, d.option_key) for d in r.decisions] == [("propose", "route_machine/5")]
    # Rejected for the right reason, not because something downstream crashed.
    reasons = [d["reason"] for kind, d in events if kind == "error" and d.get("stage") == "decide" and "attempt" in d]
    assert reasons and all("was not offered" in why for why in reasons)


def test_asking_where_the_gate_forbids_it_is_rejected(part_a, shop, chamfer):
    ask = call("decide", decisions=[{"issue_id": "I1", "action": "ask", "question": "Is it needed?", "reason": "x"}])
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "route_machine"}]),
        ask, ask,
        call("write_comment", text="Done."),
    )
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["decide"] and r.decisions[0].action == "propose"


def test_unknown_intent_lets_the_model_ask(part_a, shop, chamfer):
    model = happy_path(chamfer, {"issue_id": "I1", "action": "ask",
                                 "question": f"Is the chamfer on {chamfer} needed?", "reason": "unknown intent"})
    r = run_review(part_a, shop, model)
    assert r.fallbacks == [] and r.decisions[0].action == "ask"


def test_an_invented_number_in_the_comment_is_regenerated_then_templated(part_a, shop, chamfer):
    invented = call("write_comment", text="This saves 37 minutes per part.")
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "route_machine"}]),
        call("decide", decisions=[{"issue_id": "I1", "action": "propose", "option_key": "route_machine/5",
                                   "reason": "x"}]),
        invented, invented,
    )
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["communicate"]
    assert "37" not in r.comment and "from 2 to 1 setup(s)" in r.comment


def test_values_in_a_request_are_rejected_before_the_planner(part_a, shop, chamfer):
    with_value = call("evaluate_edits", requests=[{"kind": "set_depth", "face_group": chamfer, "value_mm": 32}])
    model = Scripted(call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
                     with_value, with_value,
                     call("decide", decisions=[{"issue_id": "I1", "action": "quiet", "reason": "x"}]),
                     call("write_comment", text="Nothing to change."))
    r = run_review(part_a, shop, model)
    assert r.fallbacks == ["options"]


def test_budget_failures_and_outages_degrade_one_stage_at_a_time(part_a, shop, chamfer):
    out_of_tokens = Reply(tool=None, arguments=None, finish_reason="length",
                          error="ran out of tokens while reasoning (budget failure)")
    model = Scripted(out_of_tokens, out_of_tokens, ModelUnavailable("HTTP 503"),
                     call("decide", decisions=[{"issue_id": "I1", "action": "propose",
                                                "option_key": "route_machine/5", "reason": "x"}]),
                     call("write_comment", text="Route to 5-axis."))
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["triage", "options"]
    assert r.decisions[0].option_key == "route_machine/5"


def test_no_model_runs_the_whole_review_as_the_fixed_script(part_a, shop, chamfer):
    r = run_review(part_a, shop, None, stated={chamfer: "Weld clearance chamfer"})
    assert r.model_calls == 0 and r.fallbacks == ["triage", "options", "decide", "communicate"]
    assert r.decisions[0].option_key == "route_machine/5"  # removal was filtered: intent is stated


# --- model client ------------------------------------------------------------------


def test_parse_reply_flags_budget_failures_and_bad_json():
    assert "budget" in parse_reply({"choices": [{"finish_reason": "length", "message": {}}]}).error
    raw = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"function": {"name": "decide", "arguments": '{"x": Infinity}'}}]}}]}
    assert "JSON" in parse_reply(raw).error
    ok = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"function": {"name": "decide", "arguments": json.dumps({"decisions": []})}}]}}]}
    assert parse_reply(ok).arguments == {"decisions": []}


def test_the_writing_stage_gets_the_larger_token_budget(part_a, shop, chamfer):
    # Live run, 2026-10-28: Spark Open ran out of tokens at 4096 writing the comment.
    model = happy_path(chamfer, {"issue_id": "I1", "action": "propose", "option_key": "route_machine/5",
                                 "reason": "x"})
    run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert [budget for _, _, budget in model.sent] == [4096, 4096, 4096, 8192]
