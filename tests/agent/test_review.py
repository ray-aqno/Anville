"""The review loop (ADR 0009) on demo Part A, with scripted models.

Part A: 2 setups (+Z, -Z), and the bottom weld-clearance chamfer is the only face in -Z.
evaluate_edits offers remove_feature (part side, 2 -> 1) and route_machine/5 (shop side, 2 -> 1).
A review is three model calls: plan, decide-and-write, verify (the verifier is a second model).
"""

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
        f"{chamfer}: {ROUTE} takes it from 2 to 1 setup(s). It changes the shop, not the part. "
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
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_with(f"Is the chamfer on {chamfer} needed?")))
    assert r.fallbacks == [] and [(d.action, d.option_key) for d in r.decisions] == [("propose", ROUTE)]
    lines = r.comment.splitlines()
    assert any(line.startswith(f"{chamfer}: {ROUTE} takes it from 2 to 1 setup(s).") for line in lines)
    assert lines[-1] == (f"{chamfer}'s purpose isn't on record. Is the chamfer on {chamfer} needed? "
                         "The answer could change the best plan.")


def test_ask_only_is_rejected_and_the_retry_must_propose(part_a, shop, chamfer):
    ask_only = decide({"issue_id": "I1", "action": "ask", "question": "Is it needed?", "reason": "x"})
    events = []
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_only, ask_with("Is it needed?")),
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
    assert BUY in r.comment and "purpose isn't on record" in r.comment


def test_a_verified_proposal_sentence_still_gets_the_question_from_code(part_a, shop, chamfer):
    said = [{"text": f"Routing to a five-axis takes the part to {{{{option.{ROUTE}.setups_after}}}} setup.",
             "role": "action", "cites": ["decision:I1", f"option:{ROUTE}"]}]
    model = Scripted(plan(*requests_for(chamfer)), decide(
        {"issue_id": "I1", "action": "propose", "option_key": ROUTE, "reason": "x", "question": "Is it needed?"},
        comment=said))
    r = run_review(part_a, shop, model, verifier=Scripted(verdicts(True), name="verifier"))
    assert "Routing to a five-axis takes the part to 1 setup." in r.comment
    assert r.comment.splitlines()[-1].endswith("Is it needed? The answer could change the best plan.")
    assert f"{ROUTE} takes it from" not in r.comment  # the verified sentence replaces the rendered line


def test_an_accepted_question_with_a_slot_is_filled_when_rendered(part_a, shop, chamfer):
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)),
                                          ask_with(f"Is {chamfer} worth {{{{plan.setups}}}} setups?")))
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


def test_when_planning_drops_everything_the_review_stops_there(part_a, shop):
    r = run_review(part_a, shop, Scripted(plan(selected=(), dropped=("I1",))))
    assert r.model_calls == 1 and r.fallbacks == [] and r.decisions == []
    assert r.comment == "Anville reviewed this version: it machines in 2 setup(s)."


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
    assert f"{chamfer}: {ROUTE} takes it from 2 to 1 setup(s)." in r.comment


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
    assert r.comment.splitlines()[-1] == (f"{chamfer}: {ROUTE} takes it from 2 to 1 setup(s). It changes the shop, "
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
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask_with("Is it needed?")))
    lines = r.comment.splitlines()
    assert lines[-2:] == [f"If {chamfer} isn't needed, {REMOVE} also takes it to 1 setup(s).",
                          f"{chamfer}'s purpose isn't on record. Is it needed? The answer could change the best plan."]
    assert sum(REMOVE in line for line in lines) == 1


def test_model_prose_cannot_offer_the_removal_when_intent_is_unknown(part_a, shop, chamfer):
    sneaky = [{"text": "Removing it would also work.", "role": "context",
               "cites": ["issue:I1", f"option:{REMOVE}"]}]
    model = Scripted(plan(*requests_for(chamfer)), decide(
        {"issue_id": "I1", "action": "propose", "option_key": ROUTE, "reason": "x", "question": "Is it needed?"},
        comment=sneaky))
    verifier = Scripted(verdicts(True), name="verifier")
    r = run_review(part_a, shop, model, verifier=verifier)
    assert "Removing it would also work." not in r.comment and verifier.sent == []  # rejected before the verifier


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
    route = run_review(part_a, costed, Scripted(plan(*requests_for(chamfer)), decide(propose_route())),
                       back_chamfer=catalog_access)
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
