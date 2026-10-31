"""The review loop (ADR 0009) on demo Part A, with scripted models.

Part A: 2 setups (+Z, -Z), and the bottom weld-clearance chamfer is the only face in -Z.
evaluate_edits offers remove_feature (part side, 2 -> 1) and route_machine/5 (shop side, 2 -> 1).
A review is three model calls: plan, decide-and-write, verify (the verifier is a second model).
"""

import re

import http.server
import json
import threading
import time
import urllib.error

import pytest

from anville.agent.model import ModelUnavailable, OpenAICompatModel, Reply, parse_reply
from anville.agent.review import (
    Intent, ask_allowed, fill_slots, filter_requests, find_issues, run_review, tag_intents,
)
from anville.geometry import extract, min_setups

ROUTE = "route_machine/5"


class Scripted:
    """A model that returns prepared tool calls in order, and records what it was sent."""

    def __init__(self, *replies, name="scripted"):
        self.replies, self.sent, self.name = list(replies), [], name

    def call(self, messages, tools, *, max_tokens=None, reasoning_effort=None, timeout_s=None):
        self.sent.append((messages, tools, max_tokens))
        self.efforts = getattr(self, "efforts", []) + [reasoning_effort]
        self.timeouts = getattr(self, "timeouts", []) + [timeout_s]
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def call(tool, **args):
    return Reply(tool=tool, arguments=args, finish_reason="tool_calls", usage={"prompt_tokens": 100})


def plan(*requests, selected=("I1",), dropped=()):
    return call("plan", selected=[{"issue_id": i, "why": "x"} for i in selected],
                dropped=[{"issue_id": i, "why": "x"} for i in dropped], requests=list(requests))


def decide(*decisions, comment=()):
    return call("decide", decisions=list(decisions), comment=list(comment))


def verdicts(*supported):
    return call("verify", verdicts=[{"index": i, "supported": ok, "why": "x"} for i, ok in enumerate(supported)])


def propose_route(reason="keep the chamfer, change the shop"):
    return {"issue_id": "I1", "action": "propose", "option_key": ROUTE, "reason": reason}


def requests_for(chamfer):
    return [{"kind": "remove_feature", "face_group": chamfer}, {"kind": "route_machine"}]


def sentences(chamfer):
    return [
        {"text": f"{chamfer} alone forces the {{{{issue.I1.directions}}}} setup.", "role": "context",
         "cites": ["issue:I1"]},
        {"text": f"Routing to a five-axis takes the part to {{{{option.{ROUTE}.setups_after}}}} setup and leaves "
                 f"{chamfer} as designed.", "role": "action",
         "cites": ["decision:I1", f"option:{ROUTE}", f"intent:{chamfer}"]},
    ]


def verifier_input(verifier):
    """The sentences the verifier was asked to judge (the JSON after the STAGE line)."""
    content = verifier.sent[0][0][1]["content"]
    return json.loads(content.split("\n\nSTAGE verify: ", 1)[1].split("\n", 1)[1])["sentences"]


# --- code stages ------------------------------------------------------------------


def test_the_bottom_chamfer_is_the_one_computed_issue(part_a, shop):
    plan_ = min_setups(part_a, shop)
    issues = find_issues(part_a, plan_)
    assert plan_.count == 2
    assert [(i.kind, i.detail["directions"]) for i in issues] == [("forces_setup", ["-Z"])]


def test_a_ledger_constraint_outranks_a_stated_reason(part_a, shop, chamfer):
    issues = find_issues(part_a, min_setups(part_a, shop))
    stated = {chamfer: "Weld clearance chamfer"}
    assert tag_intents(issues, stated)[chamfer].status == "protected"
    may = [{"id": 7, "face_group": chamfer, "rule": "may_change", "reason": "this bracket bolts to a plate"}]
    assert tag_intents(issues, stated, may)[chamfer] == Intent("free", "this bracket bolts to a plate", "ledger:7")


def test_stated_intent_is_tagged_even_for_groups_without_an_issue(part_a, shop):
    issues = find_issues(part_a, min_setups(part_a, shop))
    assert tag_intents(issues, {"G1": "datum face"})["G1"].status == "protected"


def test_part_edits_on_a_protected_face_never_reach_the_planner(chamfer):
    intents = {chamfer: Intent("protected", "Weld clearance chamfer", "cad_feature_name")}
    kept, rejected = filter_requests([{"kind": "remove_feature", "face_group": chamfer},
                                      {"kind": "add_tool", "face_group": chamfer},
                                      {"kind": "route_machine"}], intents)
    assert [r["kind"] for r in kept] == ["add_tool", "route_machine"]
    assert rejected == [{"kind": "remove_feature", "face_group": chamfer,
                         "reason": f"{chamfer} is protected: Weld clearance chamfer"}]


def test_a_part_edit_is_rejected_when_it_touches_a_protected_neighbour(part_a):
    from anville.geometry.edits import feature_faces

    groups = part_a.labelled_groups()
    named, neighbour = next((g, h) for g in groups for h in groups if h != g
                            and set(groups[h]) <= feature_faces(part_a, g))
    intents = {neighbour: Intent("protected", "sealing face", "cad_feature_name")}
    kept, rejected = filter_requests([{"kind": "remove_feature", "face_group": named}], intents, part_a)
    assert kept == [] and neighbour in rejected[0]["reason"]


def test_ask_is_allowed_only_when_the_answer_changes_the_plan(part_a, shop, chamfer):
    (issue,) = find_issues(part_a, min_setups(part_a, shop))
    assert ask_allowed(part_a, shop, issue, Intent("unknown")) is True
    assert ask_allowed(part_a, shop, issue, Intent("protected", "weld clearance")) is False


def test_the_gate_ignores_part_edits_that_would_change_a_protected_neighbour(shop, tmp_path):
    # two_pockets: removing the bottom pocket's floor feature also removes its walls. If the walls are
    # protected, removal isn't a real option, so the answer about the floor can't change the plan.
    from anville.geometry.edits import feature_faces
    from tests.geometry.parts import PARTS, write_step

    facts = extract(write_step(PARTS["two_pockets"](), tmp_path / "p.step"))
    issues = find_issues(facts, min_setups(facts, shop))
    groups = facts.labelled_groups()
    floor, walls = next((i.face_group, j.face_group) for i in issues for j in issues if i is not j
                        and set(groups[j.face_group]) <= feature_faces(facts, i.face_group))
    issue = next(i for i in issues if i.face_group == floor)
    assert ask_allowed(facts, shop, issue, Intent("unknown")) is True
    assert ask_allowed(facts, shop, issue, Intent("unknown"),
                       intents={walls: Intent("protected", "sealing walls")}) is False


def test_slots_fill_numbers_and_reject_numbers_the_model_wrote():
    values = {"plan.setups": "2", "option.route_machine/5.setups_after": "1"}
    text, err = fill_slots("G3 forces {{plan.setups}} setups; route_machine/5 gives "
                           "{{option.route_machine/5.setups_after}} on a 5-axis, from -Z.", values, [ROUTE])
    assert (text, err) == ("G3 forces 2 setups; route_machine/5 gives 1 on a 5-axis, from -Z.", "")
    assert fill_slots("Saves 37 minutes per part.", values, [ROUTE])[0] is None
    assert "unknown slot" in fill_slots("{{option.made_up.setups_after}}", values, [ROUTE])[1]


# --- the loop: three calls ------------------------------------------------------------


def test_a_stated_intent_review_publishes_verified_prose_in_three_calls(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    verifier = Scripted(verdicts(True, True), name="verifier")
    events = []
    r = run_review(part_a, shop, model, verifier=verifier, stated={chamfer: "Weld clearance chamfer"},
                   emit=lambda kind, title, **d: events.append((kind, title, d)))

    assert r.model_calls == 3 and r.fallbacks == []
    assert [u["stage"] for u in r.usage] == ["plan", "decide", "verify"]
    assert [x["kind"] for x in r.rejected] == ["remove_feature"]  # filtered before scoring
    assert r.comment.splitlines() == [
        f"{chamfer} alone forces the -Z setup.",
        f"Routing to a five-axis takes the part to 1 setup and leaves {chamfer} as designed.",
    ]
    stages = {d["stage"] for _, _, d in events}
    assert stages >= {"facts", "intent", "plan", "decide", "check", "verify", "publish"}
    assert all(not any(c.isdigit() for c in title) for _, title, _ in events)  # ADR 0007: no numbers in titles
    assert any(d.get("reason") == "keep the chamfer, change the shop" for _, _, d in events)  # reasoning in trace


def test_every_call_shares_one_prompt_prefix_for_caching(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    verifier = Scripted(verdicts(True, True), name="verifier")
    run_review(part_a, shop, model, verifier=verifier, stated={chamfer: "Weld clearance chamfer"})
    sent = [m for m, _, _ in model.sent + verifier.sent]
    assert len(sent) == 3
    assert len({m[0]["content"] for m in sent}) == 1                         # one system prompt
    assert len({m[1]["content"].split("\n\nSTAGE ")[0] for m in sent}) == 1  # one facts prefix


def test_an_unverified_action_sentence_is_replaced_by_its_rendered_line(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    r = run_review(part_a, shop, model, verifier=Scripted(verdicts(True, False), name="verifier"),
                   stated={chamfer: "Weld clearance chamfer"})
    assert r.comment.splitlines() == [
        f"{chamfer} alone forces the -Z setup.",
        f"{chamfer}: Sending it to an outside 5-axis shop takes it from 2 to 1 setup(s). It changes the shop, not the part. "
        f"{chamfer} stays as designed: Weld clearance chamfer.",
    ]


@pytest.mark.parametrize("bad, why", [
    ({"text": "It saves 37 minutes a part.", "role": "context", "cites": ["issue:I1"]}, "invented number"),
    ({"text": "Could you drop the chamfer instead?", "role": "context", "cites": ["issue:I1"]}, "question"),
    ({"text": "Or use remove_feature/{g}.", "role": "reason", "cites": ["decision:I1"]}, "stray option"),
    ({"text": "Trust me.", "role": "context", "cites": ["node:made_up"]}, "unknown node"),
    ({"text": "Routing fixes it.", "role": "action", "cites": ["issue:I1"]}, "action without its decision"),
    ({"text": "{g} is kept per intent:{g}.", "role": "reason", "cites": ["issue:I1"]}, "node id in the text"),
    ({"text": "Issue I1 forces a flip.", "role": "context", "cites": ["issue:I1"]}, "issue id in the text"),
    ({"text": "Kept for intent.{g}.reason.", "role": "reason", "cites": ["issue:I1"]}, "bare slot name"),
])
def test_code_rejects_ungrounded_sentences_before_the_verifier_sees_them(part_a, shop, chamfer, bad, why):
    bad = {**bad, "text": bad["text"].format(g=chamfer)}
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=[sentences(chamfer)[0], bad]))
    verifier = Scripted(verdicts(True), name="verifier")
    r = run_review(part_a, shop, model, verifier=verifier, stated={chamfer: "Weld clearance chamfer"})
    assert len(verifier_input(verifier)) == 1, why
    assert bad["text"] not in r.comment


def test_without_a_verifier_no_model_prose_is_published(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert "Routing to a five-axis" not in r.comment and r.model_calls == 2
    assert r.comment.splitlines()[0] == "Anville reviewed this version: it machines in 2 setup(s)."


def test_an_unavailable_verifier_publishes_no_model_prose(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    r = run_review(part_a, shop, model, verifier=Scripted(ModelUnavailable("HTTP 503"), name="verifier"),
                   stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["verify"] and "Routing to a five-axis" not in r.comment


def test_the_comment_never_carries_the_models_private_reasoning(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route("Honestly, just remove the chamfer.")))
    r = run_review(part_a, shop, model, verifier=Scripted(name="verifier"), stated={chamfer: "Weld clearance chamfer"})
    assert "remove" not in r.comment.lower() and "?" not in r.comment


def test_proposing_an_option_that_was_not_offered_is_rejected_then_falls_back(part_a, shop, chamfer):
    bad = decide({"issue_id": "I1", "action": "propose", "option_key": f"remove_feature/{chamfer}", "reason": "x"})
    model = Scripted(plan(*requests_for(chamfer)), bad, bad)
    events = []
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"},
                   emit=lambda kind, title, **d: events.append((kind, d)))
    assert r.fallbacks == ["decide"]
    assert [(d.action, d.option_key) for d in r.decisions] == [("propose", ROUTE)]
    reasons = [d["reason"] for kind, d in events if kind == "error" and d.get("stage") == "decide" and "attempt" in d]
    assert reasons and all("was not offered" in why for why in reasons)


def ask_with(question, key=ROUTE):
    """A proposal that also asks: the flag-then-suggest shape (Rayyan, 2026-10-29)."""
    return decide({"issue_id": "I1", "action": "propose", "option_key": key, "reason": "x", "question": question})


def test_a_question_where_the_gate_forbids_it_is_rejected(part_a, shop, chamfer):
    asks = ask_with("Is it needed?")
    model = Scripted(plan({"kind": "route_machine"}), asks, asks)
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["decide"] and r.decisions[0].action == "propose" and not r.decisions[0].question


def test_an_unknown_intent_is_flagged_and_a_fix_proposed_in_one_comment(part_a, shop, chamfer):
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_with(f"Is the chamfer on {chamfer} needed?")),
                   verifier=Scripted(verdicts(True), name="verifier"))
    assert r.fallbacks == [] and [(d.action, d.option_key) for d in r.decisions] == [("propose", ROUTE)]
    lines = r.comment.splitlines()
    assert any(line.startswith(f"{chamfer}: Sending it to an outside 5-axis shop takes it from 2 to 1 setup(s).") for line in lines)
    assert lines[-1] == (f"{chamfer}'s purpose isn't on record. Is the chamfer on {chamfer} needed? "
                         "The answer could change the best plan.")


def test_ask_only_is_rejected_and_the_retry_must_propose(part_a, shop, chamfer):
    ask_only = decide({"issue_id": "I1", "action": "ask", "question": "Is it needed?", "reason": "x"})
    events = []
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_only, ask_with("Is it needed?")), verifier=Scripted(verdicts(True), name="verifier"),
                   emit=lambda kind, title, **d: events.append((kind, d)))
    assert r.fallbacks == [] and r.decisions[0].action == "propose" and r.decisions[0].question == "Is it needed?"
    assert any("propose" in d.get("reason", "") for kind, d in events if kind == "error" and d.get("stage") == "decide")


def test_the_fallback_flags_and_proposes_when_intent_is_unknown(part_a, shop, chamfer, catalog_access):
    """No model: the safest fix is the shop-side one (it assumes nothing about the feature), and the
    question offers the part edit the answer could unlock."""
    (d,) = run_review(part_a, shop, None).decisions
    assert (d.action, d.option_key, bool(d.question)) == ("propose", ROUTE, True)
    r = run_review(part_a, shop, None, back_chamfer=catalog_access)
    (d,) = r.decisions
    assert (d.action, d.option_key) == ("propose", BUY) and d.question
    assert "Buying the 910480 back-chamfer cutter" in r.comment and "purpose isn't on record" in r.comment


def test_a_verified_proposal_sentence_still_gets_the_question_from_code(part_a, shop, chamfer):
    said = [{"text": f"Routing to a five-axis takes the part to {{{{option.{ROUTE}.setups_after}}}} setup.",
             "role": "action", "cites": ["decision:I1", f"option:{ROUTE}"]}]
    model = Scripted(plan(*requests_for(chamfer)), decide(
        {"issue_id": "I1", "action": "propose", "option_key": ROUTE, "reason": "x", "question": "Is it needed?"},
        comment=said))
    r = run_review(part_a, shop, model, verifier=Scripted(verdicts(True, True), name="verifier"))  # sentence, question
    assert "Routing to a five-axis takes the part to 1 setup." in r.comment
    assert r.comment.splitlines()[-1].endswith("Is it needed? The answer could change the best plan.")
    assert "Sending it to an outside 5-axis shop takes it from" not in r.comment  # the verified sentence replaces the rendered line


def test_an_accepted_question_with_a_slot_is_filled_when_rendered(part_a, shop, chamfer):
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)),
                                          ask_with(f"Is {chamfer} worth {{{{plan.setups}}}} setups?")),
                   verifier=Scripted(verdicts(True), name="verifier"))
    assert f"Is {chamfer} worth 2 setups?" in r.comment


def test_a_question_with_a_number_is_rejected_and_the_comment_is_never_empty(part_a, shop, chamfer):
    numbered = ask_with("Is the 10 mm clearance required?")
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), numbered, numbered))
    assert r.fallbacks == ["decide"]
    assert r.comment and "10 mm" not in r.comment


def test_a_stated_reason_with_numbers_is_rendered_as_data(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route()))
    r = run_review(part_a, shop, model, stated={chamfer: "1.5 mm weld clearance chamfer"})
    assert r.comment.endswith(f"{chamfer} stays as designed: 1.5 mm weld clearance chamfer.")


def test_values_in_a_request_are_rejected_before_the_planner(part_a, shop, chamfer):
    with_value = plan({"kind": "set_depth", "face_group": chamfer, "value_mm": 32})
    model = Scripted(with_value, with_value, decide({"issue_id": "I1", "action": "quiet", "reason": "x"}))
    r = run_review(part_a, shop, model)
    assert r.fallbacks == ["plan"]


def test_budget_failures_and_outages_degrade_one_stage_at_a_time(part_a, shop, chamfer):
    out_of_tokens = Reply(tool=None, arguments=None, finish_reason="length",
                          error="ran out of tokens while reasoning (budget failure)")
    model = Scripted(out_of_tokens, out_of_tokens, ModelUnavailable("HTTP 503"))
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["plan", "decide"]
    assert r.decisions[0].option_key == ROUTE  # removal was filtered: intent is stated


def test_no_model_runs_the_whole_review_as_the_fixed_script(part_a, shop, chamfer):
    r = run_review(part_a, shop, None, stated={chamfer: "Weld clearance chamfer"})
    assert r.model_calls == 0 and r.fallbacks == ["plan", "decide"]
    assert r.decisions[0].option_key == ROUTE


def test_when_planning_drops_everything_twice_the_fixed_script_raises_the_issues(part_a, shop, chamfer):
    """It used to stop at a bare setup count (2026-10-28); the gate on #26 counted that a miss."""
    empty, wrong = plan(selected=(), dropped=("I1",)), call("not_decide")
    r = run_review(part_a, shop, Scripted(empty, empty, wrong, wrong))
    assert r.fallbacks == ["plan", "decide"] and [d.issue_id for d in r.decisions] == ["I1"]
    assert any(line.startswith(f"{chamfer}: ") for line in r.comment.splitlines())


# --- model client ------------------------------------------------------------------


def test_parse_reply_flags_budget_failures_and_bad_json():
    assert "budget" in parse_reply({"choices": [{"finish_reason": "length", "message": {}}]}).error
    raw = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"function": {"name": "decide", "arguments": '{"x": Infinity}'}}]}}]}
    assert "JSON" in parse_reply(raw).error
    ok = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"function": {"name": "decide", "arguments": json.dumps({"decisions": []})}}]}}]}
    assert parse_reply(ok).arguments == {"decisions": []}


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


def test_planning_keeps_default_effort_and_deciding_runs_low(part_a, shop, chamfer):
    # Live: low effort once dropped the main issue in planning; deciding was right 4/4 at low.
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    run_review(part_a, shop, model, verifier=Scripted(verdicts(True, True), name="verifier"),
               stated={chamfer: "Weld clearance chamfer"})
    assert model.efforts == [None, "low"]


def test_planning_sees_geometry_but_not_intent(part_a, shop, chamfer):
    # Live: planning that saw "protected" dropped the main issue; intent belongs to Decide.
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route()))
    run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    plan_prompt, decide_prompt = (m[1]["content"] for m, _, _ in model.sent)
    assert "Weld clearance chamfer" not in plan_prompt and "Weld clearance chamfer" in decide_prompt



# --- Greptile on PR #14, round 4 -------------------------------------------------------


def test_an_action_sentence_that_hides_the_proposal_does_not_replace_it(part_a, shop, chamfer):
    # "G3 stays as designed." cites the decision truthfully but never states the machine change.
    hiding = {"text": f"{chamfer} stays as designed.", "role": "action", "cites": ["decision:I1", f"intent:{chamfer}"]}
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=[sentences(chamfer)[0], hiding]))
    verifier = Scripted(verdicts(True), name="verifier")
    r = run_review(part_a, shop, model, verifier=verifier, stated={chamfer: "Weld clearance chamfer"})
    assert f"{chamfer}: Sending it to an outside 5-axis shop takes it from 2 to 1 setup(s)." in r.comment


@pytest.mark.parametrize("comment", [["just text"], [{"text": "x", "role": "context"}],
                                     [{"text": 5, "role": "context", "cites": ["issue:I1"]}],
                                     [{"text": "x", "role": "boast", "cites": ["issue:I1"]}],
                                     [{"text": "x", "role": "context", "cites": "issue:I1"}]])
def test_malformed_comment_entries_are_rejected_inside_the_stage(part_a, shop, chamfer, comment):
    bad = decide(propose_route(), comment=comment)
    model = Scripted(plan(*requests_for(chamfer)), bad, bad)
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})  # must not raise
    assert r.fallbacks == ["decide"] and r.decisions[0].option_key == ROUTE


@pytest.mark.parametrize("verdict", [{"index": 0, "supported": "false", "why": "x"},
                                     {"index": 0, "why": "x"}, {"index": 0, "supported": True}])
def test_malformed_verdicts_are_rejected_and_no_prose_is_published(part_a, shop, chamfer, verdict):
    bad = call("verify", verdicts=[verdict, {"index": 1, "supported": True, "why": "x"}])
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    r = run_review(part_a, shop, model, verifier=Scripted(bad, bad, name="verifier"),
                   stated={chamfer: "Weld clearance chamfer"})  # must not raise
    assert r.fallbacks == ["verify"] and "Routing to a five-axis" not in r.comment


def test_a_protected_face_with_no_stated_reason_keeps_the_proposal(part_a, shop, chamfer):
    keep = [{"id": 3, "face_group": chamfer, "rule": "keep", "reason": ""}]
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), decide(propose_route())), constraints=keep)
    assert r.comment.splitlines()[-1] == (f"{chamfer}: Sending it to an outside 5-axis shop takes it from 2 to 1 setup(s). It changes the shop, "
                                          f"not the part. {chamfer} stays as designed.")


def test_the_verifier_sees_how_many_faces_an_option_leaves_unreachable(part_a, shop, chamfer):
    s = {"text": f"Routing leaves {{{{option.{ROUTE}.unreachable_after}}}} faces unreachable.", "role": "reason",
         "cites": [f"option:{ROUTE}"]}
    verifier = Scripted(verdicts(True), name="verifier")
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=[s]))
    run_review(part_a, shop, model, verifier=verifier, stated={chamfer: "Weld clearance chamfer"})
    assert verifier_input(verifier)[0]["cites"][f"option:{ROUTE}"]["unreachable_after"] == 0


# --- time budget (Greptile, PR #19: slow models must not cost the job its comment) ---------------


def test_a_spent_time_budget_falls_back_to_code_without_calling_a_model(part_a, shop, chamfer):
    model, verifier, events = Scripted(), Scripted(name="verifier"), []
    r = run_review(part_a, shop, model, verifier=verifier, deadline=time.monotonic(),
                   emit=lambda kind, title, **d: events.append((kind, title, d)))
    assert model.sent == [] and verifier.sent == [] and r.model_calls == 0
    assert r.fallbacks[:2] == ["plan", "decide"] and r.comment  # the code-rendered review still posts
    assert {d.get("reason") for _, t, d in events if t == "Stage fell back to the fixed script"} == {
        "time budget spent"}


def test_each_call_gets_only_the_time_left(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    verifier = Scripted(verdicts(True, True), name="verifier")
    run_review(part_a, shop, model, verifier=verifier, deadline=time.monotonic() + 100)
    assert all(90 < t <= 100 for t in model.timeouts + verifier.timeouts)


def test_the_http_timeout_is_capped_by_the_call_budget(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append(timeout)
        raise urllib.error.URLError("stop here")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    m = OpenAICompatModel("https://example.invalid/v1", "k", "m", timeout_s=120)
    for budget in (30, 500, None):
        with pytest.raises(ModelUnavailable):
            m.call([], [], timeout_s=budget)
    assert seen == [30, 120, 120]


def test_a_trickling_host_cannot_hold_a_call_past_its_budget():
    """urllib's timeout is per read: a host sending a byte every 0.5 s never trips a 1 s timeout
    (measured: 3.5 s for 8 bytes). The whole request must still end within the call's budget."""

    class Trickle(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Length", "8")
            self.end_headers()
            for _ in range(8):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.5)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m = OpenAICompatModel(f"http://127.0.0.1:{srv.server_port}/v1", "k", "m")
    t0 = time.monotonic()
    with pytest.raises(ModelUnavailable, match="no complete reply"):
        m.call([], [], timeout_s=1)
    assert time.monotonic() - t0 < 1.5
    srv.shutdown()


# --- the back-chamfer cutter the shop could buy (S3c, after PR #18) ----------------------------

BUY = "add_tool/G3/+Z/BC45-910480"


def test_the_buy_option_reaches_the_review_and_wins_when_the_chamfer_is_protected(
        part_a, shop, chamfer, catalog_access):
    stated = {chamfer: "Weld clearance chamfer"}
    without = run_review(part_a, shop, None, stated=stated)
    assert BUY not in without.options

    r = run_review(part_a, shop, None, stated=stated, back_chamfer=catalog_access)
    assert r.plan.count == 2  # the shop doesn't own the cutter yet: the base plan is unchanged
    assert (r.options[BUY].side, r.options[BUY].after.setups) == ("shop", 1)
    assert [d.option_key for d in r.decisions] == [BUY]  # the shop-side fix that keeps the chamfer


def test_the_plan_the_options_and_the_ask_gate_see_the_same_assessments(
        monkeypatch, part_a, shop, chamfer, catalog_access):
    """If they saw different lists, the base plan, the options and the gate could disagree."""
    import anville.agent.review as review

    seen = []
    for name in ("min_setups", "evaluate_edits"):
        real = getattr(review, name)

        def spy(*a, _real=real, _name=name, **k):
            seen.append((_name, k.get("back_chamfer")))
            return _real(*a, **k)

        monkeypatch.setattr(review, name, spy)
    run_review(part_a, shop, None, back_chamfer=catalog_access)  # intent unknown: the gate runs too
    assert {n for n, _ in seen} == {"min_setups", "evaluate_edits"} and len(seen) >= 3
    assert all(b is catalog_access for _, b in seen)


def test_an_assessment_survives_the_trip_from_the_geometry_image(catalog_access):
    from anville.geometry.backchamfer import BackChamferAccess

    for a in catalog_access:
        assert BackChamferAccess.from_dict(json.loads(json.dumps(a.to_dict()))) == a


def test_shop_side_fixes_are_scored_even_when_the_planner_names_only_part_edits(
        part_a, shop, chamfer, catalog_access):
    """Live, 2026-10-29 (4 of 4 runs): Plan, which sees geometry only, asked for route and remove only,
    so the 910480 was never scored or offered. Code adds the shop-side kinds for every chosen issue."""
    model = Scripted(plan({"kind": "remove_feature", "face_group": chamfer}),
                     decide({"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}))
    r = run_review(part_a, shop, model, back_chamfer=catalog_access)
    assert {BUY, ROUTE} <= set(r.options) and r.fallbacks == []
    assert [d.option_key for d in r.decisions] == [BUY]


# --- unknown intent: a fix valid under any intent leads (council ruling, accepted 2026-10-29) ------------

REMOVE = "remove_feature/G3"


def test_with_unknown_intent_a_lead_with_removal_is_rejected_for_a_shop_side_fix(part_a, shop, chamfer):
    removal = decide({"issue_id": "I1", "action": "propose", "option_key": REMOVE, "reason": "x",
                      "question": "Is it needed?"})
    events = []
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), removal, ask_with("Is it needed?")),
                   emit=lambda kind, title, **d: events.append((kind, d)))
    assert r.fallbacks == [] and r.decisions[0].option_key == ROUTE
    assert any("whatever" in d.get("reason", "") for kind, d in events if kind == "error" and d.get("stage") == "decide")


def test_removal_appears_only_as_codes_conditional_line_next_to_the_question(part_a, shop, chamfer):
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_with("Is it needed?")), verifier=Scripted(verdicts(True), name="verifier"))
    lines = r.comment.splitlines()
    assert lines[-2:] == [f"If {chamfer} isn't needed, removing {chamfer} also takes it to 1 setup(s).",
                          f"{chamfer}'s purpose isn't on record. Is it needed? The answer could change the best plan."]
    assert sum(f"removing {chamfer}" in line for line in lines) == 1


def test_model_prose_cannot_offer_the_removal_when_intent_is_unknown(part_a, shop, chamfer):
    sneaky = [{"text": "Removing it would also work.", "role": "context",
               "cites": ["issue:I1", f"option:{REMOVE}"]}]
    model = Scripted(plan(*requests_for(chamfer)), decide(
        {"issue_id": "I1", "action": "propose", "option_key": ROUTE, "reason": "x", "question": "Is it needed?"},
        comment=sneaky))
    verifier = Scripted(verdicts(True), name="verifier")
    r = run_review(part_a, shop, model, verifier=verifier)
    assert "Removing it would also work." not in r.comment and "Removing it" not in str(verifier.sent)  # rejected first


def test_a_free_face_may_still_lead_with_removal(part_a, shop, chamfer):
    may = [{"id": 7, "face_group": chamfer, "rule": "may_change", "reason": "cosmetic"}]
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), decide(
        {"issue_id": "I1", "action": "propose", "option_key": REMOVE, "reason": "x"})), constraints=may)
    assert r.fallbacks == [] and r.decisions[0].option_key == REMOVE and "isn't needed" not in r.comment


def test_sentence_events_carry_the_words_the_designer_would_read(part_a, shop, chamfer):
    events = []
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    run_review(part_a, shop, model, verifier=Scripted(verdicts(True, False), name="verifier"),
               emit=lambda kind, title, **d: events.append((title, d)))
    texts = {t: [d.get("text") for title, d in events if title == t]
             for t in ("Sentence checked", "Sentence verified", "Sentence not supported")}
    assert all(texts.values()) and all(isinstance(x, str) and x and "{{" not in x for v in texts.values() for x in v)


def test_with_unknown_intent_no_part_edit_may_lead_while_a_shop_fix_exists(part_a, shop, chamfer):
    """Codex, PR #23: barring only the conditional option let another part edit lead."""
    depth = f"set_depth/{chamfer}/+X/48"
    other = decide({"issue_id": "I1", "action": "propose", "option_key": depth, "reason": "x"})
    model = Scripted(plan(*requests_for(chamfer), {"kind": "set_depth", "face_group": chamfer}), other,
                     ask_with("Is it needed?"))
    r = run_review(part_a, shop, model)
    assert depth in r.options and r.decisions[0].option_key == ROUTE and r.fallbacks == []


def test_each_model_call_is_announced_before_it_runs(part_a, shop, chamfer):
    """Codex, PR #24: the panel must see a stage start before its slow call, not after it returns."""
    events, seen_at_call = [], []

    class Watching(Scripted):
        def call(self, *a, **k):
            seen_at_call.append(events[-1])
            return super().call(*a, **k)

    model = Watching(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    run_review(part_a, shop, model, verifier=Watching(verdicts(True, True), name="verifier"),
               emit=lambda kind, title, **d: events.append((title, d.get("stage"))))
    assert seen_at_call == [("Model called", "plan"), ("Model called", "decide"), ("Model called", "verify")]


def test_a_failed_call_still_records_its_latency(part_a, shop, chamfer):
    """Codex, PR #24: the slowest calls are the ones that time out; they belong in the timing data."""
    r = run_review(part_a, shop, Scripted(*[ModelUnavailable("no complete reply within 9 s")] * 2))
    failed = r.usage[0]
    assert failed["stage"] == "plan" and isinstance(failed["latency_s"], float) and "no complete reply" in failed["error"]


# --- dollars (S7, shop lane PR #20): code states the payback, never the model --------------------------

from anville.agent.review import payback_line  # noqa: E402
from anville.shop import DLL_COSTS, Shop  # noqa: E402


@pytest.fixture
def costed(shop):
    return Shop(tools=shop.tools, fixture=shop.fixture, name="Test shop", costs=DLL_COSTS)


def test_a_purchase_proposal_states_its_payback_range_in_code(part_a, costed, chamfer, catalog_access):
    events = []
    model = Scripted(plan(*requests_for(chamfer)),
                     decide({"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}))
    r = run_review(part_a, costed, model, back_chamfer=catalog_access,
                   emit=lambda kind, title, **d: events.append((title, d)))
    assert ("Buying the 910480 pays off past ~21 parts at mid-range inputs (anywhere from 3 to 331); "
            "below that, keep the flip.") in r.comment.splitlines()
    (buy,) = [d for t, d in events if t == "Option scored" and d["key"] == BUY]
    assert buy["payback_mid"] == 21 and buy["payback"] == [3, 331] and buy["costs"]  # the panel's dollars


def test_other_proposals_and_unpriced_shops_carry_no_dollars(part_a, shop, costed, chamfer, catalog_access):
    # Without the catalog cutter the route is Part A's only shop fix, so ADR 0010 lets it lead.
    route = run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(propose_route())))
    assert "pays off" not in route.comment
    buy = decide({"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"})
    plain = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), buy), back_chamfer=catalog_access)
    assert plain.decisions[0].option_key == BUY and "pays off" not in plain.comment


@pytest.mark.parametrize("payback, mid, line", [
    ((None, None), None, "Buying the {{option.K.tool}} doesn't pay off at these inputs."),
    ((3, None), 21, "Buying the {{option.K.tool}} pays off past ~{{option.K.payback_mid}} parts at mid-range "
                    "inputs (from {{option.K.payback_lo}} at best; at the worst inputs it never does); "
                    "below that, keep the flip."),
    ((3, 331), None, "Buying the {{option.K.tool}} pays off past {{option.K.payback_lo}} parts at the best "
                     "inputs, but never at mid-range ones."),
])
def test_payback_wording_never_invents_an_end_of_the_range(payback, mid, line):
    assert payback_line("K", payback, mid) == line


def test_no_payback_is_claimed_against_a_plan_that_cant_make_the_part(monkeypatch, part_a, costed, chamfer,
                                                                       catalog_access):
    """Codex, PR #26: a route that leaves faces unreachable has no break-even quantity."""
    import anville.shop as shop_mod

    real = shop_mod.cost_options

    def incomplete_current(*a, **k):
        result = real(*a, **k)
        result.routes[0].complete = False
        return result

    monkeypatch.setattr(shop_mod, "cost_options", incomplete_current)
    buy = decide({"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"})
    r = run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), buy), back_chamfer=catalog_access)
    assert r.decisions[0].option_key == BUY and "pays off" not in r.comment


def test_the_cost_comparison_and_its_inputs_reach_the_trace(part_a, costed, chamfer, catalog_access):
    """Codex, PR #26: the panel must show that the verdict depends, and which inputs are placeholders."""
    events = []
    run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(propose_route())),
               emit=lambda kind, title, **d: events.append((title, d)))  # route is the only shop fix (ADR 0010)
    (compared,) = [d for t, d in events if t == "Costs compared"]
    assert {v["quantity"] for v in compared["verdicts"]} == {4, 10, 20}
    assert any(i["status"] == "placeholder" for i in compared["inputs"].values())


# --- unknown intent with no shop-side fix (Rayyan's ruling, 2026-10-30: lanes 1 and 3) ------------------

BORE_REMOVE, BORE_ROUTE = "remove_feature/G9", "route_machine/5"


def bore_plan():
    return plan({"kind": "remove_feature", "face_group": "G9"}, {"kind": "route_machine"}, dropped=("I2",))


def test_a_proposal_that_changes_nothing_is_rejected(undercut, shop):
    """Gate, PR #23: 2 of 9 runs proposed the 5-axis route, which leaves the bore unreachable
    ("takes it from 1 to 1 setup(s)")."""
    nothing = decide({"issue_id": "I1", "action": "propose", "option_key": BORE_ROUTE, "reason": "x"})
    events = []
    r = run_review(undercut, shop, Scripted(bore_plan(), nothing, nothing),
                   emit=lambda kind, title, **d: events.append((title, d)))
    assert r.fallbacks == ["decide"] and BORE_ROUTE not in [d.option_key for d in r.decisions]
    assert any("changes nothing" in d.get("reason", "") for t, d in events if t == "Model answer rejected")


def test_with_no_shop_fix_the_review_flags_and_asks_and_removal_is_only_conditional(undercut, shop):
    r = run_review(undercut, shop, None)
    d = next(x for x in r.decisions if x.issue_id == "I1")
    assert (d.action, d.option_key) == ("ask", None) and d.question
    lines = r.comment.splitlines()
    assert "G9: only a part edit fixes this, and its purpose isn't on record." in lines
    assert "If G9 isn't needed, removing G9 takes it to 0 unreachable face(s)." in lines
    assert not any(line.startswith("G9: Removing G9") for line in lines)  # never leads unconditionally


def test_the_model_may_ask_only_where_no_shop_fix_exists(undercut, shop, part_a, chamfer):
    lead_removal = decide({"issue_id": "I1", "action": "propose", "option_key": BORE_REMOVE, "reason": "x",
                           "question": "Is G9 needed?"})
    ask = decide({"issue_id": "I1", "action": "ask", "question": "Is G9 needed?", "reason": "x"})
    r = run_review(undercut, shop, Scripted(bore_plan(), lead_removal, ask))
    assert r.fallbacks == [] and r.decisions[0].action == "ask"
    ask_a = decide({"issue_id": "I1", "action": "ask", "question": "Is it needed?", "reason": "x"})
    a = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_a, ask_with("Is it needed?")))
    assert a.decisions[0].action == "propose"  # Part A has a shop fix: asking alone is still rejected


def test_a_fix_for_unreachable_faces_says_so(undercut, shop):
    free = [{"id": 1, "face_group": "G9", "rule": "may_change", "reason": "cosmetic"}]
    r = run_review(undercut, shop, Scripted(bore_plan(), decide(
        {"issue_id": "I1", "action": "propose", "option_key": BORE_REMOVE, "reason": "x"})), constraints=free)
    assert "G9: Removing G9 takes it from 2 to 0 unreachable face(s)." in r.comment.splitlines()


def test_with_no_shop_fix_the_conditional_edit_carries_its_dollar_estimate(undercut, costed):
    """Rayyan's ruling, 2026-10-30, lane 3: estimate the change with the cost model, from slots."""
    r = run_review(undercut, costed, None)
    q = max(costed.costs.scenarios.quantities)
    line = next(line for line in r.comment.splitlines() if line.startswith(f"At {q} parts,"))
    assert "removing G9" in line and "$" in line and "designer time isn't priced" in line


def test_only_plan_changing_options_are_priced(part_a, costed, chamfer, catalog_access):
    events = []
    run_review(part_a, costed, Scripted(plan(*requests_for(chamfer), {"kind": "set_depth", "face_group": chamfer}),
                                        decide(propose_route())),
               emit=lambda kind, title, **d: events.append((title, d)))  # route is the only shop fix (ADR 0010)
    scored = [d for t, d in events if t == "Option scored"]
    assert all(("costs" in d) == d["changes_plan"] for d in scored) and any(not d["changes_plan"] for d in scored)


def test_rendered_comments_never_show_option_keys(part_a, undercut, costed, catalog_access):
    """Shop lane's demo check of #26: the fallback posted "add_tool/G3/+Z/BC45-910480 takes it from"."""
    for r in (run_review(part_a, costed, None, back_chamfer=catalog_access), run_review(undercut, costed, None)):
        assert r.comment and not any(key in r.comment for key in r.options), r.comment
    assert "Buying the 910480 back-chamfer cutter takes it from 2 to 1 setup(s)." in \
        run_review(part_a, costed, None, back_chamfer=catalog_access).comment


def test_a_tool_only_change_is_said_as_one():
    """Codex, PR #23: a fix that changes only the cutting tool read "takes it from 1 to 1 setup(s)"."""
    from types import SimpleNamespace as NS

    from anville.agent.review import _effect

    o = NS(key="set_min_radius/G1/r6", before=NS(setups=1, unreachable=0), after=NS(setups=1, unreachable=0),
           tool_changes=[NS(face_id=4, before="EM3", after="EM12")])
    assert _effect(o) == "changes the tool that cuts it and keeps {{option.set_min_radius/G1/r6.setups_after}} setup(s)"


def test_the_rules_allow_the_one_ask_the_tool_offers():
    """Codex, PR #23: RULES said a question never replaces a proposal, which contradicts `ask_only`."""
    from anville.agent.review import RULES

    assert "ask_only" in RULES


def test_every_catalog_kind_has_a_plain_name():
    """Codex, PR #26 (P1): set_min_radius fell through to its option key."""
    from types import SimpleNamespace as NS

    from anville.agent.review import _name
    from anville.geometry.edits import CATALOG

    for kind in CATALOG:
        name = _name(NS(kind=kind, key=f"{kind}/G1/x", face_group="G1", params={"name": "EM6"}))
        prose = re.sub(r"\{\{[^}]*\}\}", "", name)  # slots carry keys by design; the prose must not
        assert "/" not in prose and kind not in prose, (kind, name)


def test_the_estimate_says_its_rates_are_placeholders(undercut, costed):
    """Codex, PR #26: most DLL inputs are placeholders, and the comment must say so where it shows dollars."""
    line = next(x for x in run_review(undercut, costed, None).comment.splitlines() if x.startswith("At "))
    assert "placeholder shop rates" in line


def test_part_edits_are_scored_where_the_answer_could_change_the_plan(undercut, shop):
    """Gate on #26: Plan asked for the route only, so no part edit was scored, `ask_only` stayed empty
    and the review went quiet on the undercut (3 of 10). Like the shop-side kinds, code scores them."""
    wrong = call("not_decide")
    r = run_review(undercut, shop, Scripted(plan({"kind": "route_machine"}, dropped=("I2",)), wrong, wrong))
    assert BORE_REMOVE in r.options
    assert "If G9 isn't needed, removing G9 takes it to 0 unreachable face(s)." in r.comment.splitlines()


def test_a_plan_that_raises_nothing_is_retried(part_a, shop, chamfer):
    """Gate on #26: Plan selected nothing in 2 of 30 Part A reviews, and the comment only counted setups.
    Code found the issue, so an empty selection is rejected and the stage retries."""
    model = Scripted(plan(selected=(), dropped=("I1",)), plan(*requests_for(chamfer)), decide(propose_route()))
    r = run_review(part_a, shop, model)
    assert "plan" not in r.fallbacks and "Your previous answer was rejected" in model.sent[1][0][1]["content"]
    assert any(line.startswith(f"{chamfer}: ") for line in r.comment.splitlines())


def test_a_plan_cut_off_for_length_retries_at_low_effort(part_a, shop, chamfer):
    """Gate on #26: two_pockets' Plan hit its cap twice at the default effort. RFC 0001's budget
    policy: the retry runs at lower effort."""
    cut = Reply(tool=None, arguments=None, finish_reason="length",
                error="ran out of tokens while reasoning (budget failure)")
    model = Scripted(cut, plan(*requests_for(chamfer)), decide(propose_route()))
    run_review(part_a, shop, model)
    assert model.efforts[:2] == [None, "low"]


def test_one_route_is_said_once(shop, tmp_path):
    """Gate on #26: the fallback said "Sending it to an outside 5-axis shop" for G8 and again for G10."""
    from tests.geometry.parts import PARTS, write_step

    facts = extract(write_step(PARTS["two_pockets"](), tmp_path / "p.step"))
    lines = run_review(facts, shop, None).comment.splitlines()
    assert sum("outside 5-axis shop" in line for line in lines) <= 1
    assert sum(line.endswith("the same change covers it.") for line in lines) >= 1, lines


# --- ADR 0010: cost-ranked any-intent fixes (mid-range tie-break, Rayyan 2026-10-30) ----------------------


def test_the_ranked_lead_is_the_only_shop_fix_that_may_lead(part_a, costed, chamfer, catalog_access):
    """Gate on #26: with intent stated, Part A led with the 5-axis route 10 of 10 and the 910480 0 of 10.
    On DLL inputs the 910480 is cheaper at mid-range inputs at every quantity, so it leads."""
    buy = {"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route()), decide(buy))
    r = run_review(part_a, costed, model, back_chamfer=catalog_access, stated={chamfer: "Weld clearance chamfer"})
    assert r.decisions[0].option_key == BUY and "decide" not in r.fallbacks
    assert "costs less at mid-range inputs" in model.sent[2][0][1]["content"]


def test_the_fallback_leads_with_the_ranked_fix(part_a, costed, chamfer, catalog_access):
    r = run_review(part_a, costed, None, back_chamfer=catalog_access, stated={chamfer: "Weld clearance chamfer"})
    assert r.decisions[0].option_key == BUY


def test_a_tie_break_lead_names_its_rival_and_never_claims_cheapest(part_a, costed, chamfer, catalog_access):
    """ADR 0010 §5: the lead wins only at mid-range inputs, so the comment says the route can cost less,
    and model prose may not call the lead cheapest."""
    r = run_review(part_a, costed, None, back_chamfer=catalog_access, stated={chamfer: "Weld clearance chamfer"})
    assert "Sending it to an outside 5-axis shop costs less at some inputs." in r.comment.splitlines()
    cheapest = {"text": "Buying the cutter is the cheapest fix.", "role": "action", "cites": ["decision:I1"]}
    buy = {"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}
    model = Scripted(plan(*requests_for(chamfer)), decide(buy, comment=[cheapest]), decide(buy))
    run_review(part_a, costed, model, back_chamfer=catalog_access, stated={chamfer: "Weld clearance chamfer"})
    assert "cheapest" in model.sent[2][0][1]["content"]


def test_each_issue_is_ranked_over_its_own_fixes(part_a, costed, chamfer, catalog_access):
    events = []
    run_review(part_a, costed, None, back_chamfer=catalog_access, stated={chamfer: "Weld clearance chamfer"},
               emit=lambda kind, title, **d: events.append((title, d)))
    (ranked,) = [d for t, d in events if t == "Fixes ranked"]
    assert ranked["issue_id"] == "I1" and ranked["lead"] == BUY and ranked["basis"] == "mid_range"
    assert set(ranked["top"]) == {BUY, ROUTE}


# --- Rayyan's ruling (a), 2026-10-30: the intent question is always attached where it could change the plan ---


def _buy(question=None):
    d = {"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}
    return {**d, "question": question} if question else d


def test_code_attaches_the_intent_question_when_the_model_leaves_it_out(part_a, costed, chamfer, catalog_access):
    """Gate on #37: on the live (unknown-intent) path the model asked in 1 of 20 runs, so the comment never invited
    the reply the re-plan beat needs."""
    r = run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(_buy())), back_chamfer=catalog_access)
    lines = r.comment.splitlines()
    assert r.decisions[0].question == f"Is {chamfer} required as modelled?"
    assert f"If {chamfer} isn't needed, removing {chamfer} also takes it to 1 setup(s)." in lines
    assert lines[-1].endswith("The answer could change the best plan.")


def test_a_model_question_is_kept_only_if_the_verifier_supports_it(part_a, costed, chamfer, catalog_access):
    """Shop lane, 2026-10-31: the model's question went out after only a slot check, so "no model prose without
    a verifier" was false for the ask line. Now it is verified like a sentence; otherwise code's question goes."""
    mine = f"Does the weld fixture need {chamfer}?"
    for verifier, expected in ((Scripted(verdicts(True), name="verifier"), mine),
                               (Scripted(verdicts(False), name="verifier"), f"Is {chamfer} required as modelled?"),
                               (None, f"Is {chamfer} required as modelled?")):
        r = run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(_buy(mine))),
                       verifier=verifier, back_chamfer=catalog_access)
        assert r.decisions[0].question == expected
        assert sum("modelled?" in line or mine in line for line in r.comment.splitlines()) == 1


def test_no_question_is_attached_when_intent_is_stated(part_a, costed, chamfer, catalog_access):
    r = run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(_buy())),
                   back_chamfer=catalog_access, stated={chamfer: "Weld clearance chamfer"})
    assert r.decisions[0].question is None


def test_one_fix_covering_two_issues_asks_once(shop, tmp_path):
    """The ruling's one intent question: two_pockets' fallback asked about G8 and again about G10."""
    from tests.geometry.parts import PARTS, write_step

    facts = extract(write_step(PARTS["two_pockets"](), tmp_path / "p.step"))
    r = run_review(facts, shop, None)
    assert sum(d.question is not None for d in r.decisions) == 1
    assert sum("required as modelled?" in line for line in r.comment.splitlines()) == 1


def test_option_scored_carries_the_plain_name_and_params(part_a, shop, catalog_access):
    """Panel lane: it re-derived names from keys, and an owned cutter ("use the shop's") can't be told
    from the key. The trace now carries review's own name, slots filled, plus kind and params."""
    events = []
    run_review(part_a, shop, None, back_chamfer=catalog_access,
               emit=lambda kind, title, **d: events.append((title, d)))
    scored = {d["key"]: d for t, d in events if t == "Option scored"}
    buy = next(d for k, d in scored.items() if k.startswith("add_tool/"))
    assert buy["name"] == "buying the 910480 back-chamfer cutter" and buy["edit_kind"] == "add_tool"
    assert buy["params"]["owned"] is False and "{{" not in "".join(d["name"] for d in scored.values())


# --- Rayyan, 2026-10-31: once the designer frees a face, its part edits are ranked with the shop fixes ----------


def _freed(chamfer, **kw):
    return [{"id": 1, "face_group": chamfer, "rule": "may_change", "reason": "not needed", **kw}]


def test_a_freed_chamfers_removal_is_ranked_and_leads(part_a, costed, chamfer, catalog_access):
    """Gate on #37: after "not needed", turn 2 bought the $243 cutter 10 of 10 times; nothing ranked the free
    removal against it. The designer's own edit time is theirs to give, so it no longer counts against it."""
    removal = f"remove_feature/{chamfer}"
    events = []
    r = run_review(part_a, costed, None, back_chamfer=catalog_access, constraints=_freed(chamfer),
                   emit=lambda kind, title, **d: events.append((title, d)))
    (ranked,) = [d for t, d in events if t == "Fixes ranked"]
    assert ranked["lead"] == removal and BUY not in ranked["top"]
    assert r.decisions[0].option_key == removal
    assert "Sending it to an outside 5-axis shop costs less at some inputs." in r.comment.splitlines()


def test_the_model_may_not_lead_with_the_cutter_over_a_freed_removal(part_a, costed, chamfer, catalog_access):
    removal = f"remove_feature/{chamfer}"
    buy = {"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}
    rm = {"issue_id": "I1", "action": "propose", "option_key": removal, "reason": "x"}
    model = Scripted(plan(*requests_for(chamfer)), decide(buy), decide(rm))
    r = run_review(part_a, costed, model, back_chamfer=catalog_access, constraints=_freed(chamfer))
    assert r.decisions[0].option_key == removal and "decide" not in r.fallbacks
    assert removal in model.sent[2][0][1]["content"]  # the retry was told which fix leads


def test_a_designer_who_explicitly_wants_the_shop_fix_gets_it(part_a, costed, chamfer, catalog_access):
    """'Unless the designer is clearly explicit': a reply that frees the face but asks for the cutter keeps the
    ranking over the shop fixes, as before."""
    events = []
    r = run_review(part_a, costed, None, back_chamfer=catalog_access,
                   constraints=_freed(chamfer, prefer="shop_fix", prefer_reason="buy the cutter anyway"),
                   emit=lambda kind, title, **d: events.append((title, d)))
    (ranked,) = [d for t, d in events if t == "Fixes ranked"]
    assert ranked["lead"] == BUY and not any(k.startswith("remove_feature/") for k in ranked["top"])
    assert r.decisions[0].option_key == BUY


def test_a_freed_faces_part_edits_are_scored_even_if_plan_asks_only_for_shop_fixes(part_a, costed, chamfer,
                                                                                    catalog_access):
    """Codex, PR #41 (P1): the removal was offered only when Plan asked for it."""
    removal = f"remove_feature/{chamfer}"
    rm = {"issue_id": "I1", "action": "propose", "option_key": removal, "reason": "x"}
    shop_only = plan({"kind": "route_machine"}, {"kind": "add_tool", "face_group": chamfer})
    r = run_review(part_a, costed, Scripted(shop_only, decide(rm)), back_chamfer=catalog_access,
                   constraints=_freed(chamfer))
    assert removal in r.options and r.decisions[0].option_key == removal


def test_an_explicit_side_with_no_fix_falls_back_to_the_other_side():
    """Codex, PR #41: asking for a part edit when none changes the plan must not leave the review quiet."""
    from types import SimpleNamespace as NS

    from anville.agent.review import _fallback_decision

    shop_fix = NS(key="route_machine/5", side="shop", face_group=None, changes_plan=True)
    no_help = NS(key="set_depth/G3", side="part", face_group="G3", changes_plan=False)
    issue = NS(id="I1", face_group="G3")
    d = _fallback_decision(issue, False, [no_help, shop_fix], prefer="part_edit")
    assert (d.action, d.option_key) == ("propose", "route_machine/5")
    helps = NS(key="remove_feature/G3", side="part", face_group="G3", changes_plan=True)
    assert _fallback_decision(issue, False, [shop_fix, helps], prefer="part_edit").option_key == "remove_feature/G3"


# --- Models lane, 2026-10-31: cost claims had no node to cite, so the verifier dropped all of them -------------


def test_the_ranking_and_each_options_price_are_citable(part_a, costed, chamfer, catalog_access):
    """Decide saw cost_ranking and paybacks, but a sentence about them could cite only option nodes with setup
    counts, so the verifier found 'no cited node supports' every cost claim (21 of 26 drops)."""
    cost_line = {"text": "Of the fixes that work whatever G3 is for, buying the cutter costs the least at "
                         "mid-range shop rates.", "role": "context", "cites": ["cost:I1"]}
    buy = {"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}
    model = Scripted(plan(*requests_for(chamfer)), decide(buy, comment=[cost_line]))
    verifier = Scripted(verdicts(True), name="verifier")
    run_review(part_a, costed, model, verifier=verifier, back_chamfer=catalog_access)
    extra = lambda m, i: json.loads(m.sent[i][0][1]["content"].split("\n", 3)[-1].split("\n\nYour previous")[0])  # noqa: E731
    cites = extra(verifier, 0)["sentences"][0]["cites"]
    assert cites["cost:I1"]["lead"] == BUY and cites["cost:I1"]["basis"] == "mid_range"
    assert "midpoint" in cites["cost:I1"]["means"]
    assert "cost:I1" in extra(model, 1)["nodes"]


def test_a_replaced_question_reaches_the_panel_and_no_prose_rests_on_it(part_a, costed, chamfer, catalog_access):
    """Codex, PR #45: the panel's card kept showing the rejected question, and a sentence could be verified
    against a decision node that carried it."""
    mine = f"Does the weld fixture need {chamfer}?"
    line = {"text": f"Buying the cutter takes it to {{{{option.{BUY}.setups_after}}}} setup(s).", "role": "action",
            "cites": ["decision:I1", f"option:{BUY}"]}
    events = []
    verifier = Scripted(verdicts(True, False), name="verifier")  # the sentence is fine; the question isn't
    run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(_buy(mine), comment=[line])),
               verifier=verifier, back_chamfer=catalog_access, emit=lambda kind, title, **d: events.append((title, d)))
    made = [d for t, d in events if t == "Decision made"]
    assert made[-1]["question"] == f"Is {chamfer} required as modelled?" and made[-1]["question_replaced"]
    sent = json.loads(verifier.sent[0][0][1]["content"].split("\n", 3)[-1])["sentences"]
    assert sent[0]["cites"]["decision:I1"] == {"action": "propose", "option_key": BUY}  # no question to lean on
    assert sent[1]["role"] == "question" and sent[1]["text"] == mine
