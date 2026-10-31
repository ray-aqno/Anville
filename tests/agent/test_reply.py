"""ADR 0012: a designer's reply becomes constraints. The model reads the reply; code accepts a rule only for a
face group the review raised, and only with the designer's own words as its reason."""

from anville.agent.reply import constraint_entry, constraints_from_ledger, nothing_read, read_reply
from anville.agent.review import run_review
from tests.agent.test_review import ROUTE, Scripted, call, decide, plan, requests_for

RAISED = {"G3": "forces a setup"}
REPLY = "Keep G3, it's the weld clearance for the frame tube."


def rules(*items):
    return call("read_reply", rules=list(items))


def keep(quote="it's the weld clearance for the frame tube", group="G3"):
    return {"face_group": group, "rule": "keep", "quote": quote}


def test_a_grounded_rule_is_accepted():
    found, _ = read_reply(Scripted(rules(keep())), REPLY, RAISED, emit=lambda *a, **k: None)
    assert found == [{"face_group": "G3", "rule": "keep", "quote": "it's the weld clearance for the frame tube"}]


def test_a_quote_the_designer_never_wrote_is_rejected_and_retried():
    model = Scripted(rules(keep("it's needed for the bearing seat")), rules(keep()))
    found, _ = read_reply(model, REPLY, RAISED, emit=lambda *a, **k: None)
    assert found[0]["quote"] == "it's the weld clearance for the frame tube"
    assert "the designer's own words" in model.sent[1][0][1]["content"]


def test_quotes_match_across_case_and_spacing():
    found, _ = read_reply(Scripted(rules(keep("KEEP G3,  it's"))), REPLY, RAISED, emit=lambda *a, **k: None)
    assert found and found[0]["rule"] == "keep"


def test_a_group_the_review_never_raised_is_rejected():
    model = Scripted(rules(keep(group="G7")), rules(keep(group="G7")))
    found, run = read_reply(model, REPLY, RAISED, emit=lambda *a, **k: None)
    assert found is None and run.fallbacks == ["read_reply"]


def test_a_reply_that_decides_nothing_reads_as_no_rules():
    found, _ = read_reply(Scripted(rules()), "Thanks, looking at it tomorrow.", RAISED, emit=lambda *a, **k: None)
    assert found == []


def test_no_model_falls_back_to_reading_nothing():
    found, run = read_reply(None, REPLY, RAISED, emit=lambda *a, **k: None)
    assert found is None and run.fallbacks == ["read_reply"]


def test_a_rule_becomes_a_ledger_entry_and_back_into_a_constraint(part_a, shop, chamfer):
    entry = constraint_entry(keep(group=chamfer), reply_id="c-reply", review_key="version:v1", version_id="v1")
    assert entry["provenance"] == {"comment": "c-reply", "review": "version:v1"}
    stored = [{"id": 7, "ledger": "constraint", "scope": "doc:d", "created_at": 0.0, **entry}]
    (c,) = constraints_from_ledger(stored)
    assert c == {"id": 7, "face_group": chamfer, "rule": "keep", "reason": "it's the weld clearance for the frame tube",
                 "version_id": "v1"}
    # Turn 2: the kept chamfer is protected, so removal is filtered and the shop-side fix leads.
    r = run_review(part_a, shop, Scripted(plan(*requests_for(chamfer)),
                                          decide({"issue_id": "I1", "action": "propose", "option_key": ROUTE,
                                                  "reason": "x"})), constraints=[c])
    assert r.intents[chamfer].status == "protected" and r.decisions[0].option_key == ROUTE


def test_nothing_read_names_what_stays_open():
    assert nothing_read(["G3"], 2) == "Anville read this reply, but not as a decision on G3; the plan stands at 2 setup(s)."
    assert nothing_read(["G3", "G6"], 1).startswith("Anville read this reply, but not as a decision on G3 or G6;")
