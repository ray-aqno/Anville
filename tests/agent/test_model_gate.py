"""The eval gate's scorer (scripts/evals/score.py) on hand-built review results: no models, no geometry."""

from types import SimpleNamespace as NS

from scripts.evals import score

BUY, ROUTE, REMOVE = "add_tool/G3/+Z/BC45-910480", "route_machine/5", "remove_feature/G3"
OPTIONS = {BUY: NS(side="shop", changes_plan=True, face_group="G3"),
           ROUTE: NS(side="shop", changes_plan=True, face_group=None),
           REMOVE: NS(side="part", changes_plan=True, face_group="G3")}
VALUES = {"plan.setups": "2", f"option.{BUY}.setups_after": "1", f"option.{BUY}.setups_before": "2"}


def result(*decisions, comment="", fallbacks=(), options=OPTIONS, issues=(NS(id="I1", face_group="G3"),),
           calls=3):
    return NS(decisions=list(decisions), comment=comment, fallbacks=list(fallbacks), options=options,
              issues=list(issues), model_calls=calls)


def propose(key, question=None):
    return NS(issue_id="I1", action="propose", option_key=key, question=question)


def test_survival_counts_only_the_model_stages():
    assert score.survived(result(fallbacks=["verify"]))
    assert not score.survived(result(fallbacks=["decide"]))
    assert not score.survived(result(fallbacks=["plan"]))


def test_a_question_without_a_proposal_is_a_bare_question():
    assert score.no_bare_question(result(propose(BUY, "Is G3 required?")))
    assert not score.no_bare_question(result(NS(issue_id="I1", action="ask", option_key=None, question="Is G3?")))
    assert not score.no_bare_question(result(NS(issue_id="I1", action="quiet", option_key=None, question="Is G3?")))


def test_numbers_must_come_from_slot_values():
    ok = f"It machines in 2 setup(s).\nG3: {BUY} takes it from 2 to 1 setup(s). Cuts from +Z on a 5-axis."
    assert score.numbers_from_code(result(comment=ok), VALUES)  # digits in keys, labels and axes don't count
    assert not score.numbers_from_code(result(comment="G3 adds 3 minutes."), VALUES)


def test_any_intent_fix_leads_or_does_not_apply():
    assert score.any_intent_leads(result(propose(BUY)), "G3")
    assert not score.any_intent_leads(result(propose(REMOVE)), "G3")
    only_part = {REMOVE: OPTIONS[REMOVE]}
    assert score.any_intent_leads(result(propose(REMOVE), options=only_part), "G3") is None  # undercut case


def test_a_shop_option_that_changes_nothing_is_not_a_fix():
    """Codex, P1: a no-op shop proposal must fail even while a plan-changing one is offered."""
    noop = {**OPTIONS, ROUTE: NS(side="shop", changes_plan=False, face_group=None)}
    assert not score.shop_fix(result(propose(ROUTE), options=noop), "G3")
    assert not score.any_intent_leads(result(propose(ROUTE), options=noop), "G3")
    assert score.any_intent_leads(result(propose(BUY), options=noop), "G3")


def test_an_unpriced_model_stops_the_gate_instead_of_costing_nothing():
    """Codex, P2: a zero cost would let spend run past the budget unseen."""
    import pytest
    from scripts.evals import gate

    assert gate.cost([{"model": "muse-spark-1.3", "prompt_tokens": 1_000_000, "completion_tokens": 0}]) == 1.25
    with pytest.raises(ValueError, match="no price"):
        gate.cost([{"model": "some-new-model", "prompt_tokens": 10, "completion_tokens": 10}])


BORE, BORE_OUT = "G9", "remove_feature/G9"
UNDERCUT = {BORE_OUT: NS(side="part", changes_plan=True, face_group=BORE),
            ROUTE: NS(side="shop", changes_plan=False, face_group=None)}


def undercut(*decisions, comment=""):
    return NS(decisions=list(decisions), comment=comment, fallbacks=[], options=UNDERCUT, model_calls=3,
              issues=[NS(id="I1", face_group=BORE)], selected=["I1"], ask_allowed={"I1": True},
              intents={BORE: NS(status="unknown")})


def ask(question="Is G9 required?"):
    return NS(issue_id="I1", action="ask", option_key=None, question=question)


def test_asking_alone_is_allowed_only_where_removal_is_the_only_fix():
    """Rayyan's ruling, 2026-10-30, lane 1: no shop fix and a part-only fix, so flag and ask."""
    assert score.ask_only(undercut()) == {"I1"}
    assert score.no_bare_question(undercut(ask()))
    assert not score.no_bare_question(result(NS(issue_id="I1", action="ask", option_key=None, question="Is G3?"),
                                             options=OPTIONS))  # a shop fix exists: still a bare question


def test_where_only_removal_helps_the_review_asks_and_never_leads_with_it():
    assert score.asks_without_removal(undercut(ask()))
    assert not score.asks_without_removal(undercut(NS(issue_id="I1", action="propose", option_key=BORE_OUT,
                                                      question="Is G9 required?")))
    assert not score.asks_without_removal(undercut(NS(issue_id="I1", action="quiet", option_key=None,
                                                      question=None)))
    assert score.asks_without_removal(result(propose(BUY))) is None  # not that case


def test_going_quiet_where_only_removal_helps_fails_even_if_removal_was_never_scored():
    """Live, 2026-10-30: Plan didn't request remove_feature in 3 of 10 undercut runs, so no part fix was
    offered, asking wasn't allowed, and both issues went quiet. asks_without_removal saw no such issue
    (None); flags_and_asks judges the outcome."""
    quiet = undercut(NS(issue_id="I1", action="quiet", option_key=None, question=None))
    quiet.options = {ROUTE: UNDERCUT[ROUTE]}  # removal never scored
    assert score.asks_without_removal(quiet) is None
    assert not score.flags_and_asks(quiet)
    assert score.flags_and_asks(undercut(ask()))


def test_the_estimate_must_reach_the_comment_when_code_priced_it():
    """Lane 3: the dollars of the part edit, filled from slots."""
    values = {f"option.{BORE_OUT}.cost_lo": "267", f"option.{BORE_OUT}.cost_hi": "2,400"}
    line = "At 20 parts, the plan after removing G9 runs $267–$2,400; designer time isn't priced."
    assert score.estimate_shown(undercut(ask(), comment=line), values)
    assert not score.estimate_shown(undercut(ask(), comment="Is G9 required?"), values)
    assert score.estimate_shown(undercut(ask()), {}) is None  # unpriced shop: nothing to show


def test_removal_is_judged_from_the_decision_not_keys_in_the_text():
    """Rendered lines no longer carry option keys (main, 2026-10-30): "removing G3", not remove_feature/G3."""
    line = "If G3 isn't needed, removing G3 also takes it to 1 setup(s)."
    assert score.removal_conditional_only(result(propose(BUY, "Is G3 required?"), comment=line), "G3")
    assert not score.removal_conditional_only(result(propose(BUY), comment=line), "G3")  # no question
    assert not score.removal_conditional_only(result(propose(REMOVE, "Is G3 required?")), "G3")  # leads


def test_the_cutter_part_number_in_a_rendered_name_is_code_not_the_model():
    text = "G3: Buying the 910480 back-chamfer cutter takes it from 2 to 1 setup(s)."
    assert score.numbers_from_code(result(comment=text), VALUES)


def test_the_lead_is_scored_against_the_trace_ranking_not_a_fixed_key():
    """ADR 0010 on #37: the cost-ranked shop fix leads; the ranking comes from the "Fixes ranked" event."""
    assert score.ranked_lead(result(propose(BUY)), "G3", {"I1": BUY})
    assert not score.ranked_lead(result(propose(ROUTE)), "G3", {"I1": BUY})
    assert score.ranked_lead(result(propose(ROUTE)), "G3", {}) is None  # code under test doesn't rank
    # The ranking orders shop-side fixes; after "not needed", a part edit may lead (dry run, #37).
    assert score.ranked_lead(result(propose(REMOVE)), "G3", {"I1": BUY}) is None


def test_a_dropped_or_quiet_issue_is_not_raised():
    assert score.raised(result(propose(ROUTE)), "G3")
    assert not score.raised(result(NS(issue_id="I1", action="quiet", option_key=None, question=None)), "G3")
    assert not score.raised(result(), "G3")


def test_the_same_fix_proposed_for_two_issues_counts_once_as_repeated():
    two = result(propose(ROUTE), NS(issue_id="I2", action="propose", option_key=ROUTE, question=None))
    assert score.repeated_fixes(two) == 1


def test_the_question_check_flags_one_that_names_a_fix():
    catalog = ("set_min_radius", "remove_feature", "route_machine")
    assert score.question_names_no_fix("Is G3 required as modelled?", catalog, "G3")
    assert not score.question_names_no_fix("Could we remove feature G3?", catalog, "G3")
    assert score.question_names_no_fix(None, catalog, "G3") is None


def test_unknown_intent_with_the_gate_open_always_asks():
    """Rayyan's ruling (a), 2026-10-30: code attaches the intent question, so a miss is a bug."""
    def unknown(*decisions, allowed=True):
        r = result(*decisions)
        r.intents, r.ask_allowed = {"G3": NS(status="unknown")}, {"I1": allowed}
        return r
    assert score.intent_asked(unknown(propose(BUY, "Is G3 required as modelled?")), "G3")
    assert not score.intent_asked(unknown(propose(BUY)), "G3")
    assert score.intent_asked(unknown(propose(BUY), allowed=False), "G3") is None  # gate closed
    stated = unknown(propose(BUY))
    stated.intents = {"G3": NS(status="stated")}
    assert score.intent_asked(stated, "G3") is None


def test_on_a_freed_face_the_ranked_lead_may_be_the_part_edit():
    """ADR 0010 Amendment 1 (2026-10-31): a freed face's removal is ranked with the shop fixes, so the lead is
    scored on either side. On code that ranks only shop fixes, a part-side lead still isn't scored."""
    assert score.ranked_lead(result(propose(REMOVE)), "G3", {"I1": REMOVE}, freed={"I1"})
    assert not score.ranked_lead(result(propose(BUY)), "G3", {"I1": REMOVE}, freed={"I1"})
    assert score.ranked_lead(result(propose(REMOVE)), "G3", {"I1": BUY}) is None
