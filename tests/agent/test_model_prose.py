"""Code is the backstop, not the author (Rayyan, 2026-10-31: "constrain the accuracy, not the wording"): a code
line is added only when verified model prose didn't already say it. Numbers stay code-filled slots."""

import pytest

import anville.agent.review as review
import json
from anville.agent.review import run_review
from tests.agent.test_review import BUY, ROUTE, Scripted, costed, decide, plan, requests_for, verdicts  # noqa: F401

STATED = "Weld clearance chamfer"
PROPOSE_BUY = {"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}


def context(chamfer):
    return {"text": f"{chamfer} alone forces the {{{{issue.I1.directions}}}} setup.", "role": "context",
            "cites": ["issue:I1"]}


def buy_with_payback(chamfer):
    return {"text": f"Buying the {{{{option.{BUY}.tool}}}} takes the part to {{{{option.{BUY}.setups_after}}}} "
                    f"setup and pays off past about {{{{option.{BUY}.payback_mid}}}} parts at mid-range shop rates, "
                    f"anywhere from {{{{option.{BUY}.payback_lo}}}} to {{{{option.{BUY}.payback_hi}}}}.",
            "role": "action", "cites": ["decision:I1", f"option:{BUY}"]}


def rival(chamfer):
    return {"text": f"{{{{option.{ROUTE}.fix}}}} can still come out ahead at some shop rates.", "role": "reason",
            "cites": [f"option:{ROUTE}", "cost:I1"]}


def review_with(part_a, costed, chamfer, catalog_access, comment):
    model = Scripted(plan(*requests_for(chamfer)), decide(PROPOSE_BUY, comment=comment))
    verifier = Scripted(verdicts(*[True] * len(comment)), name="verifier")
    return run_review(part_a, costed, model, verifier=verifier, back_chamfer=catalog_access,
                      stated={chamfer: STATED})


def test_code_states_the_payback_only_when_the_models_verified_prose_didnt(part_a, costed, chamfer,
                                                                            catalog_access):
    said = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), buy_with_payback(chamfer)])
    lines = said.comment.splitlines()
    assert any("pays off past about" in ln for ln in lines)  # the model's sentence, slots filled by code
    assert not any(ln.startswith("Buying the 910480 pays off past") for ln in lines)  # code's line, not needed

    unsaid = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer)])
    assert any(ln.startswith("Buying the 910480 pays off past") for ln in unsaid.comment.splitlines())


def test_code_names_the_rival_only_when_the_models_verified_prose_didnt(part_a, costed, chamfer, catalog_access):
    code_rival = "Sending it to an outside 5-axis shop costs less at some inputs."
    said = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), rival(chamfer)])
    assert code_rival not in said.comment.splitlines()
    unsaid = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer)])
    assert code_rival in unsaid.comment.splitlines()


def test_a_payback_without_its_range_leaves_code_to_say_it(part_a, costed, chamfer, catalog_access):
    """ADR 0010 §5: the range is what says the payback depends on inputs. Prose that gives only the midpoint
    doesn't cover the line: code's line says every slot it would, or it stays."""
    mid_only = {**buy_with_payback(chamfer), "text": f"Buying the {{{{option.{BUY}.tool}}}} takes the part to "
                f"{{{{option.{BUY}.setups_after}}}} setup and pays off past about {{{{option.{BUY}.payback_mid}}}} "
                "parts at mid-range shop rates."}
    r = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), mid_only])
    assert any(ln.startswith("Buying the 910480 pays off past") for ln in r.comment.splitlines())


def test_a_dropped_sentence_leaves_code_to_say_it(part_a, costed, chamfer, catalog_access):
    """The backstop counts verified prose only: a payback sentence the verifier rejects doesn't cover it."""
    model = Scripted(plan(*requests_for(chamfer)), decide(PROPOSE_BUY, comment=[context(chamfer),
                                                                               buy_with_payback(chamfer)]))
    r = run_review(part_a, costed, model, verifier=Scripted(verdicts(True, False), name="verifier"),
                   back_chamfer=catalog_access, stated={chamfer: STATED})
    assert any(ln.startswith("Buying the 910480 pays off past") for ln in r.comment.splitlines())


def test_a_face_can_be_described_from_measured_geometry(monkeypatch, part_a, costed, chamfer, catalog_access):
    """geometry's describe_group gives a code-measured phrase; it reaches prose through a slot, digits included."""
    monkeypatch.setattr(review, "describe_group", lambda facts, group: "the 1.5 mm chamfer on the bottom front edge")
    where = {"text": "It is {{issue.I1.where}} that forces its own setup.", "role": "context", "cites": ["issue:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [where])
    assert "It is the 1.5 mm chamfer on the bottom front edge that forces its own setup." in r.comment.splitlines()


@pytest.mark.parametrize("found", [None, ""])
def test_without_a_description_no_slot_is_offered(monkeypatch, part_a, costed, chamfer, catalog_access, found):
    monkeypatch.setattr(review, "describe_group", lambda facts, group: found)
    events = []
    model = Scripted(plan(*requests_for(chamfer)), decide(PROPOSE_BUY))
    run_review(part_a, costed, model, back_chamfer=catalog_access, stated={chamfer: STATED},
               emit=lambda kind, title, **d: events.append((title, d)))
    assert "issue.I1.where" not in str(model.sent[1])


# The two live G1 comments (2026-10-31, as posted; sheet links shortened). The model wrote A's first 3 lines
# and B's first 1; code wrote the rest.
DEMO_A = """G11 alone forces a second setup in the -Z direction, taking the plan to 2 setups.
G11 has no stated functional intent, so its purpose is unknown and geometry changes are negotiable.
Adding tool 910480 from +Z takes it to 1 setup(s) with no part change.
Buying the 910480 pays off past ~21 parts at mid-range inputs (anywhere from 3 to 331); below that, keep the flip.
Sending it to an outside 5-axis shop costs less at some inputs.
If G11 isn't needed, removing G11 also takes it to 1 setup(s).
G11's purpose isn't on record. Is G11 required as modelled? The answer could change the best plan.
Setup sheet for the shop: <link>"""
DEMO_B = """G10 is currently machined from -Z requiring 2 setups, and its purpose is unknown.
G10: Buying the 910480 back-chamfer cutter takes it from 2 to 1 setup(s). It changes the shop, not the part.
Buying the 910480 pays off past ~21 parts at mid-range inputs (anywhere from 3 to 331); below that, keep the flip.
Sending it to an outside 5-axis shop costs less at some inputs.
If G10 isn't needed, removing G10 also takes it to 1 setup(s).
G10's purpose isn't on record. Is G10 required as modelled? The answer could change the best plan.
Setup sheet for the shop: <link>
Reviewed by Muse Spark Open (Baseten) · checked by Muse Spark 1.3 (Meta API) · 3 model calls · 22 s"""


def test_the_share_of_lines_the_model_wrote_on_the_live_comments():
    """The prose metric. The setup-sheet link and the credits line are code's by design, not review content,
    so they count in neither total."""
    from scripts.evals.score import model_line_share

    assert model_line_share(DEMO_A, model_lines=3) == (3, 7)
    assert model_line_share(DEMO_B, model_lines=1) == (1, 6)


def test_an_option_node_says_what_the_fix_is_so_prose_naming_it_can_be_verified(part_a, costed, chamfer,
                                                                                  catalog_access):
    """Live, 2026-10-31: "Cited nodes show key route_machine/5 ... but never state 5-axis; term unsupported"."""
    from anville.agent.review import comment_nodes

    r = run_review(part_a, costed, None, back_chamfer=catalog_access, stated={chamfer: STATED})
    nodes = comment_nodes(r.plan, [i for i in r.issues if i.id in r.selected], r.intents, r.options)
    assert nodes[f"option:{ROUTE}"]["fix"] == "sending it to an outside 5-axis shop"
    assert nodes[f"option:{BUY}"]["fix"] == "buying the 910480 back-chamfer cutter"


def _sent_to_verifier(verifier):
    import json

    content = verifier.sent[0][0][1]["content"]  # base JSON, the stage's instructions, then the stage's JSON
    return json.loads(content.rsplit("\n", 1)[1])["sentences"]


def test_a_sentence_citing_a_decision_brings_the_decided_fix_to_the_verifier(part_a, costed, chamfer,
                                                                             catalog_access):
    """Live, 2026-10-31: "shop-side ... unsupported; side lives in option node not cited here"."""
    shop_side = {"text": "This shop-side change leaves the part as designed.", "role": "reason",
                 "cites": ["decision:I1", "cost:I1"]}
    model = Scripted(plan(*requests_for(chamfer)), decide(PROPOSE_BUY, comment=[shop_side]))
    verifier = Scripted(verdicts(True), name="verifier")
    run_review(part_a, costed, model, verifier=verifier, back_chamfer=catalog_access, stated={chamfer: STATED})
    (sent,) = _sent_to_verifier(verifier)
    assert sent["cites"][f"option:{BUY}"]["side"] == "shop"


def test_the_verifier_is_told_a_question_rides_on_the_proposal(part_a, costed, chamfer, catalog_access):
    """Ruling (a): the intent question is attached to the proposal, not an ask. Live, 2026-10-31, the verifier
    rejected purpose questions as "asks beyond accepted propose decision with no ask approval"."""
    question = "What is G3 for, and is it needed as modelled?"
    model = Scripted(plan(*requests_for(chamfer)), decide({**PROPOSE_BUY, "question": question}))
    verifier = Scripted(verdicts(True), name="verifier")
    r = run_review(part_a, costed, model, verifier=verifier, back_chamfer=catalog_access)
    (sent,) = _sent_to_verifier(verifier)
    assert sent["role"] == "question"
    assert sent["cites"]["decision:I1"] == {"action": "propose", "option_key": BUY, "carries_this_question": True}
    assert question in r.comment


def test_the_verifiers_accepted_decisions_show_which_carries_a_question(part_a, costed, chamfer, catalog_access):
    """Live, 2026-10-31 (after the cites fix): "accepted decision is propose without question flag, so asking goes
    beyond it". The list the verifier judges against must say the proposal carries the question, and why."""
    import json

    question = "What is G3 for, and is it needed as modelled?"
    model = Scripted(plan(*requests_for(chamfer)), decide({**PROPOSE_BUY, "question": question}))
    verifier = Scripted(verdicts(True), name="verifier")
    run_review(part_a, costed, model, verifier=verifier, back_chamfer=catalog_access)
    content = verifier.sent[0][0][1]["content"]
    accepted = json.loads(content.rsplit("\n", 1)[1])["accepted_decisions"]
    assert accepted == [{"issue_id": "I1", "action": "propose", "option_key": BUY, "carries_question": True}]
    assert "purpose is unknown" in content.rsplit("\n", 1)[0]  # the instruction states ruling (a)


def test_a_geometry_error_never_costs_the_review(monkeypatch, part_a, costed, chamfer, catalog_access):
    """Geometry lane: describe_group raises ValueError on an unknown label. The face just goes undescribed."""
    import anville.geometry

    def broken(facts, group):
        raise ValueError(f"no group {group}")

    monkeypatch.setattr(anville.geometry, "describe_group", broken, raising=False)
    assert review.describe_group(part_a, "G3") is None
    r = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer)])
    assert r.comment


def test_a_sentence_that_opens_with_a_description_is_capitalised(monkeypatch, part_a, costed, chamfer,
                                                                 catalog_access):
    """describe_group's phrases start lowercase ("the left Ø8.5 through hole")."""
    monkeypatch.setattr(review, "describe_group", lambda facts, group: "the 1.5 mm chamfer on the bottom front edge")
    opener = {"text": "{{issue.I1.where}} forces its own setup.", "role": "context", "cites": ["issue:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [opener])
    assert "The 1.5 mm chamfer on the bottom front edge forces its own setup." in r.comment.splitlines()


def test_the_model_names_a_fix_in_codes_own_words_through_a_slot(part_a, costed, chamfer, catalog_access):
    """Live, take g2's re-plan: "Adding the +Z angled tool keeps the shop routing while preserving geometry ..."
    never named the cutter. The fix's name is a slot, so the model can say it exactly, digits included."""
    named = {"text": f"{{{{option.{BUY}.fix}}}} takes it to {{{{option.{BUY}.setups_after}}}} setup(s).",
             "role": "action", "cites": ["decision:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [named])
    assert "Buying the 910480 back-chamfer cutter takes it to 1 setup(s)." in r.comment.splitlines()
    assert "{{option.<key>.fix}}" in json.dumps(review.DECIDE_TOOL)


def test_a_rival_named_through_its_fix_slot_covers_codes_rival_line(part_a, costed, chamfer, catalog_access):
    """Live, 2026-10-31: "... leads at mid-range shop rates over sending it to an outside 5-axis shop", and code
    still added "Sending it to an outside 5-axis shop costs less at some inputs.": the rival twice."""
    ranked = {"text": f"{{{{option.{ROUTE}.fix}}}} can come out ahead at some shop rates.", "role": "reason",
              "cites": ["decision:I1", "cost:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [ranked])
    assert "Sending it to an outside 5-axis shop can come out ahead at some shop rates." in r.comment.splitlines()
    assert "costs less at some inputs." not in r.comment


def test_a_lead_ahead_of_a_rival_doesnt_say_the_rival_wins_elsewhere(part_a, costed, chamfer, catalog_access):
    """Codex, second pass: "leads at mid-range rates, ahead of X" never says X costs less at some inputs."""
    lead = {"text": f"It leads at mid-range shop rates, ahead of {{{{option.{ROUTE}.fix}}}}.", "role": "reason",
            "cites": ["decision:I1", "cost:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), lead])
    assert "Sending it to an outside 5-axis shop costs less at some inputs." in r.comment.splitlines()


def test_a_payback_that_never_pays_off_somewhere_is_always_codes_line():
    """Codex, P2: a None end means code's line says "never" at those inputs; prose using every numeric slot
    can still leave that out, so it can't cover the line."""
    k = BUY
    said = [f"It pays off past ~{{{{option.{k}.payback_mid}}}} parts ({{{{option.{k}.payback_lo}}}} to "
            f"{{{{option.{k}.payback_hi}}}})."]
    assert review.payback_covered(k, (3, 331), 21, said)
    assert not review.payback_covered(k, (3, None), 21, said)
    assert not review.payback_covered(k, (3, 331), None, said)
    # Codex, third pass: the three numbers listed, or split across sentences, don't state the payback
    listed = [f"Inputs: {{{{option.{k}.payback_lo}}}}, {{{{option.{k}.payback_mid}}}}, {{{{option.{k}.payback_hi}}}}."]
    assert not review.payback_covered(k, (3, 331), 21, listed)
    split = [f"It pays off past ~{{{{option.{k}.payback_mid}}}} parts.",
             f"The range is {{{{option.{k}.payback_lo}}}} to {{{{option.{k}.payback_hi}}}}."]
    assert not review.payback_covered(k, (3, 331), 21, split)


def test_the_chosen_fix_winning_at_some_rates_doesnt_cover_the_rival(part_a, costed, chamfer, catalog_access):
    """Codex, third pass: the covering sentence must be about the rival, opening with its own name."""
    ours = {"text": f"{{{{option.{BUY}.fix}}}} wins at some shop rates; we also weighed {{{{option.{ROUTE}.fix}}}}.",
            "role": "reason", "cites": [f"option:{ROUTE}", "cost:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), ours])
    assert "Sending it to an outside 5-axis shop costs less at some inputs." in r.comment.splitlines()


def test_naming_a_rival_without_comparing_it_leaves_codes_rival_line(part_a, costed, chamfer, catalog_access):
    """Codex, P2: "We also considered ..." cites the rival and the ranking but never says it can come out ahead."""
    mention = {"text": f"We also considered {{{{option.{ROUTE}.fix}}}}.", "role": "reason",
               "cites": [f"option:{ROUTE}", "cost:I1"]}
    r = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), mention])
    assert "Sending it to an outside 5-axis shop costs less at some inputs." in r.comment.splitlines()


def test_a_rival_said_to_cost_more_doesnt_cover_its_line(part_a, costed, chamfer, catalog_access):
    """Codex, fourth pass: "at some shop rates" with the opposite claim isn't the caveat."""
    opposite = {**rival(chamfer), "text": f"{{{{option.{ROUTE}.fix}}}} costs more at some shop rates."}
    r = review_with(part_a, costed, chamfer, catalog_access, [context(chamfer), opposite])
    assert "Sending it to an outside 5-axis shop costs less at some inputs." in r.comment.splitlines()
