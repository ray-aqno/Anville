"""The review loop (ADR 0009) on demo Part A, with scripted models.

Part A: 2 setups (+Z, -Z), and the bottom weld-clearance chamfer is the only face in -Z.
evaluate_edits offers remove_feature (part side, 2 -> 1) and route_machine/5 (shop side, 2 -> 1).
A review is three model calls: plan, decide-and-write, verify (the verifier is a second model).
"""

import json

import pytest

from anville.agent.model import ModelUnavailable, Reply, parse_reply
from anville.agent.review import (
    Intent, ask_allowed, fill_slots, filter_requests, find_issues, run_review, tag_intents,
)
from anville.geometry import extract, min_setups

ROUTE = "route_machine/5"


class Scripted:
    """A model that returns prepared tool calls in order, and records what it was sent."""

    def __init__(self, *replies, name="scripted"):
        self.replies, self.sent, self.name = list(replies), [], name

    def call(self, messages, tools, *, max_tokens=None, reasoning_effort=None):
        self.sent.append((messages, tools, max_tokens))
        self.efforts = getattr(self, "efforts", []) + [reasoning_effort]
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


def test_asking_where_the_gate_forbids_it_is_rejected(part_a, shop, chamfer):
    ask = decide({"issue_id": "I1", "action": "ask", "question": "Is it needed?", "reason": "x"})
    model = Scripted(plan({"kind": "route_machine"}), ask, ask)
    r = run_review(part_a, shop, model, stated={chamfer: "Weld clearance chamfer"})
    assert r.fallbacks == ["decide"] and r.decisions[0].action == "propose"


def test_an_accepted_ask_is_placed_by_code_with_its_reason(part_a, shop, chamfer):
    ask = decide({"issue_id": "I1", "action": "ask", "reason": "x", "question": f"Is the chamfer on {chamfer} needed?"})
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask))
    assert r.fallbacks == [] and r.decisions[0].action == "ask"
    assert r.comment.splitlines()[-1] == (f"{chamfer}: Is the chamfer on {chamfer} needed? Its purpose isn't on "
                                          "record, and the answer changes the best plan.")


def test_an_accepted_question_with_a_slot_is_filled_when_rendered(part_a, shop, chamfer):
    ask = decide({"issue_id": "I1", "action": "ask", "reason": "x",
                  "question": f"Is {chamfer} worth {{{{plan.setups}}}} setups?"})
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)), ask))
    assert f"{chamfer}: Is {chamfer} worth 2 setups?" in r.comment


def test_a_question_with_a_number_is_rejected_and_the_comment_is_never_empty(part_a, shop, chamfer):
    numbered = decide({"issue_id": "I1", "action": "ask", "question": "Is the 10 mm clearance required?",
                       "reason": "x"})
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
