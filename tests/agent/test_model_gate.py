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


def test_removal_may_only_appear_as_the_conditional_line_with_a_question():
    line = f"If G3 isn't needed, {REMOVE} also takes it to 1 setup(s)."
    assert score.removal_conditional_only(result(propose(BUY, "Is G3 required?"), comment=line), "G3")
    assert not score.removal_conditional_only(result(propose(BUY), comment=line), "G3")  # no question
    assert not score.removal_conditional_only(result(propose(BUY), comment=f"Or try {REMOVE}."), "G3")


def test_any_intent_fix_leads_or_does_not_apply():
    assert score.any_intent_leads(result(propose(BUY)), "G3")
    assert not score.any_intent_leads(result(propose(REMOVE)), "G3")
    only_part = {REMOVE: OPTIONS[REMOVE]}
    assert score.any_intent_leads(result(propose(REMOVE), options=only_part), "G3") is None  # undercut case


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
