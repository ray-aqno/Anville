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


# --- Codex, PR #38 ------------------------------------------------------------------------------------------

def test_own_comments_are_recognised_without_ephemeral_state():
    """P1: if the in-memory self: entry is lost, Anville's re-plan would read as a designer reply and loop."""
    from anville.agent.reply import is_own_comment
    from anville.connectors.onshape.oauth import KEYS_HEADER

    posted = {"c-replan": ("comment:c-reply", "replan")}.get
    assert is_own_comment({"id": "c-replan", "message": "x"}, posted)
    assert is_own_comment({"id": "c-new", "message": KEYS_HEADER + "posted on the owner's keys"}, posted)
    assert not is_own_comment({"id": "c-designer", "message": "keep it"}, posted)


def test_a_reply_under_a_replan_finds_its_review_through_durable_records():
    """P1: the thread map lived only in ephemeral state."""
    from anville.agent.reply import resolve_review

    posts = {"c-root": ("version:v1", "review"), "c-replan": ("comment:c-reply1", "replan")}.get
    parents = {"c-reply1": "c-root"}
    get_parent = parents.get
    assert resolve_review("c-root", posts, get_parent) == "version:v1"
    assert resolve_review("c-replan", posts, get_parent) == "version:v1"
    assert resolve_review("c-elsewhere", posts, get_parent) is None
    loop = {"c-a": ("comment:c-b", "replan"), "c-b": ("comment:c-a", "replan")}.get
    assert resolve_review("c-a", loop, {"c-b": "c-a", "c-a": "c-b"}.get) is None  # bounded, never spins


def test_only_constraints_from_the_reviewed_version_apply():
    """P1: face labels are stable only within one STEP, so another version's G3 must not protect this one's."""
    rows = [{"id": 1, "payload": {"face_group": "G3", "rule": "keep", "reason": "a", "version_id": "v1"},
             "provenance": {}},
            {"id": 2, "payload": {"face_group": "G3", "rule": "may_change", "reason": "b", "version_id": "v2"},
             "provenance": {}}]
    assert [c["id"] for c in constraints_from_ledger(rows, version_id="v1")] == [1]


def test_a_retried_reply_appends_nothing_twice():
    """P1: a job retried after append_ledger would re-append, and the old rule would become the newest."""
    from anville.agent.reply import unrecorded

    entry = constraint_entry(keep(), reply_id="c-reply", review_key="version:v1", version_id="v1")
    stored = [{"id": 5, **entry}]
    assert unrecorded([entry], stored) == []
    other = constraint_entry(keep(group="G6"), reply_id="c-reply", review_key="version:v1", version_id="v1")
    assert unrecorded([entry, other], stored) == [other]


# --- Rayyan, 2026-10-31: "unless the designer is clearly explicit" -------------------------------------------

def test_an_explicit_side_is_read_only_with_the_designers_own_words(part_a, shop, chamfer):
    reply = "G3 isn't needed, but buy the cutter anyway: we'll want it on the next bracket."
    wants = {"face_group": chamfer, "rule": "may_change", "quote": "isn't needed",
             "prefer": "shop_fix", "prefer_quote": "buy the cutter anyway"}
    found, _ = read_reply(Scripted(rules(wants)), reply, {chamfer: "x"}, emit=lambda *a, **k: None)
    assert found == [wants]
    entry = constraint_entry(wants, reply_id="c", review_key="version:v1", version_id="v1")
    assert entry["payload"]["prefer"] == "shop_fix" and entry["payload"]["prefer_reason"] == "buy the cutter anyway"
    (c,) = constraints_from_ledger([{"id": 1, **entry}])
    assert c["prefer"] == "shop_fix"


def test_an_invented_preference_is_refused(part_a, shop, chamfer):
    reply = "G3 isn't needed."
    made_up = {"face_group": chamfer, "rule": "may_change", "quote": "isn't needed",
               "prefer": "shop_fix", "prefer_quote": "buy the cutter"}
    no_quote = {**made_up, "prefer_quote": ""}
    on_keep = {"face_group": chamfer, "rule": "keep", "quote": "isn't needed", "prefer": "part_edit",
               "prefer_quote": "isn't needed"}
    for bad in (made_up, no_quote, on_keep):
        found, run = read_reply(Scripted(rules(bad), rules(bad)), reply, {chamfer: "x"}, emit=lambda *a, **k: None)
        assert found is None and run.fallbacks == ["read_reply"], bad
