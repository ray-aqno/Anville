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
        call("write_comment", intro=f"{chamfer} alone forces the {{{{issue.I1.directions}}}} setup.",
             reasons=[{"issue_id": "I1", "why": "The shop changes; the part doesn't."}]),
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
                         "reason": f"{chamfer} is protected: Weld clearance chamfer"}]


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
    assert r.comment.splitlines()[1].startswith(f"{chamfer}: route_machine/5 takes it from 2 to 1 setup(s).")
    assert r.comment.splitlines()[0] == f"{chamfer} alone forces the -Z setup."
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
        call("write_comment", intro="One issue.", reasons=[{"issue_id": "I1", "why": "Keeps the chamfer."}]),
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
        call("write_comment", intro="Done.", reasons=[{"issue_id": "I1", "why": "Shop side."}]),
    )
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["decide"] and r.decisions[0].action == "propose"


def test_unknown_intent_lets_the_model_ask(part_a, shop, chamfer):
    model = happy_path(chamfer, {"issue_id": "I1", "action": "ask",
                                 "question": f"Is the chamfer on {chamfer} needed?", "reason": "unknown intent"})
    r = run_review(part_a, shop, model)
    assert r.fallbacks == [] and r.decisions[0].action == "ask"


def test_an_invented_number_in_the_comment_is_regenerated_then_templated(part_a, shop, chamfer):
    invented = call("write_comment", intro="This saves 37 minutes per part.",
                    reasons=[{"issue_id": "I1", "why": "x"}])
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
                     call("write_comment", intro="Nothing to change.", reasons=[{"issue_id": "I1", "why": "x"}]))
    r = run_review(part_a, shop, model)
    assert r.fallbacks == ["options"]


def test_budget_failures_and_outages_degrade_one_stage_at_a_time(part_a, shop, chamfer):
    out_of_tokens = Reply(tool=None, arguments=None, finish_reason="length",
                          error="ran out of tokens while reasoning (budget failure)")
    model = Scripted(out_of_tokens, out_of_tokens, ModelUnavailable("HTTP 503"),
                     call("decide", decisions=[{"issue_id": "I1", "action": "propose",
                                                "option_key": "route_machine/5", "reason": "x"}]),
                     call("write_comment", intro="Route to 5-axis.", reasons=[{"issue_id": "I1", "why": "x"}]))
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


# --- Greptile on PR #14 ---------------------------------------------------------


def test_a_part_edit_is_rejected_when_it_touches_a_protected_neighbour(part_a):
    # A part edit acts on a feature: the named group plus groups sharing a concave edge with it.
    from anville.geometry.edits import feature_faces

    groups = part_a.labelled_groups()
    named, neighbour = next((g, h) for g in groups for h in groups if h != g
                            and set(groups[h]) <= feature_faces(part_a, g))
    intents = {neighbour: Intent("protected", "sealing face", "cad_feature_name")}
    kept, rejected = filter_requests([{"kind": "remove_feature", "face_group": named}], intents, part_a)
    assert kept == [] and neighbour in rejected[0]["reason"]


def test_stated_intent_is_tagged_even_for_groups_without_an_issue(part_a, shop, chamfer):
    issues = find_issues(part_a, min_setups(part_a, shop))
    assert tag_intents(issues, {"G1": "datum face"})["G1"].status == "protected"


def test_a_question_in_the_comment_needs_an_ask_decision(part_a, shop, chamfer):
    asks = call("write_comment", intro=f"Can you remove the chamfer on {chamfer}?",
                reasons=[{"issue_id": "I1", "why": "x"}])
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "route_machine"}]),
        call("decide", decisions=[{"issue_id": "I1", "action": "propose", "option_key": "route_machine/5",
                                   "reason": "x"}]),
        asks, asks,
    )
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["communicate"] and "?" not in r.comment


def test_a_question_with_a_number_is_rejected_and_the_comment_is_never_empty(part_a, shop, chamfer):
    numbered = call("decide", decisions=[{"issue_id": "I1", "action": "ask",
                                          "question": "Is the 10 mm clearance required?", "reason": "x"}])
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "remove_feature", "face_group": chamfer},
                                         {"kind": "route_machine"}]),
        numbered, numbered,
        ModelUnavailable("HTTP 503"),
    )
    r = run_review(part_a, shop, model)
    assert r.fallbacks == ["decide", "communicate"]
    assert r.comment and "10 mm" not in r.comment


@pytest.mark.parametrize("body", [b"not json", b'{"choices": []}', b'{"no_choices": 1}'])
def test_a_malformed_host_reply_becomes_model_unavailable(monkeypatch, body):
    from anville.agent import model as m

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda req, timeout: Resp())
    with pytest.raises(ModelUnavailable):
        m.OpenAICompatModel("https://x.invalid/v1", "k", "model").call([], [])


def test_when_triage_drops_everything_the_review_stops_there(part_a, shop):
    # Live run, 2026-10-28: the model dropped the only issue, and the later stages failed on nothing.
    model = Scripted(call("triage", selected=[], dropped=[{"issue_id": "I1", "why": "minor"}]))
    r = run_review(part_a, shop, model)
    assert r.model_calls == 1 and r.fallbacks == [] and r.decisions == []
    assert r.comment == "Anville reviewed this version: it machines in 2 setup(s)."


# --- Greptile on PR #14, round 2 -------------------------------------------------


def test_actions_in_the_comment_are_rendered_from_accepted_decisions(part_a, shop, chamfer):
    # The model writes only the framing; it can't put "remove the chamfer" in as an action.
    model = happy_path(chamfer, {"issue_id": "I1", "action": "propose", "option_key": "route_machine/5",
                                 "reason": "x"})
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    actions = r.comment.splitlines()[1:]
    assert len(actions) == 1 and "remove_feature" not in r.comment


def test_a_reason_that_asks_a_question_is_rejected(part_a, shop, chamfer):
    sneaky = call("write_comment", intro="Review.", reasons=[{"issue_id": "I1", "why": "Could you drop it?"}])
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "route_machine"}]),
        call("decide", decisions=[{"issue_id": "I1", "action": "propose", "option_key": "route_machine/5",
                                   "reason": "x"}]),
        sneaky, sneaky,
    )
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["communicate"] and "?" not in r.comment


def test_an_accepted_question_with_a_slot_survives_the_fallback(part_a, shop, chamfer):
    ask = call("decide", decisions=[{"issue_id": "I1", "action": "ask", "reason": "x",
                                     "question": f"Is {chamfer} worth {{{{plan.setups}}}} setups?"}])
    model = Scripted(
        call("triage", selected=[{"issue_id": "I1", "why": "x"}], dropped=[]),
        call("evaluate_edits", requests=[{"kind": "remove_feature", "face_group": chamfer},
                                         {"kind": "route_machine"}]),
        ask, ModelUnavailable("HTTP 503"),
    )
    r = run_review(part_a, shop, model)
    assert r.fallbacks == ["communicate"]
    assert f"{chamfer}: Is {chamfer} worth 2 setups?" in r.comment


def test_the_gate_ignores_part_edits_that_would_change_a_protected_neighbour(shop, tmp_path):
    # two_pockets: the bottom pocket's floor and walls force the -Z setup. Removing the floor's
    # feature also removes the walls, so if the walls are protected, removal isn't a real option
    # and the answer about the floor can't change the plan.
    from anville.geometry.edits import feature_faces
    from tests.geometry.parts import PARTS, write_step

    facts = extract(write_step(PARTS["two_pockets"](), tmp_path / "p.step"))
    issues = find_issues(facts, min_setups(facts, shop))
    groups = facts.labelled_groups()
    floor, walls = next((i.face_group, j.face_group) for i in issues for j in issues if i is not j
                        and set(groups[j.face_group]) <= feature_faces(facts, i.face_group))
    issue = next(i for i in issues if i.face_group == floor)
    assert ask_allowed(facts, shop, issue, Intent("unknown")) is True
    protected = {walls: Intent("protected", "sealing walls")}
    assert ask_allowed(facts, shop, issue, Intent("unknown"), intents=protected) is False
