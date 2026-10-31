"""The agent inside the deployed review job (S3b): never costs the designer a review, and leaves a
per-call record (stage, model, host, tokens) for the trace."""

import time

from anville.agent.job import SHOP_SCOPE, agent_comment, catalog_for, flushing_at_stage_changes, review_shop
from anville.agent.model import OpenAICompatModel
from tests.agent.test_review import (
    Scripted, decide, plan, propose_route, requests_for, sentences, verdicts,
)


def events_into(events):
    return lambda kind, title, **d: events.append((kind, title, d))


def test_the_agent_is_skipped_when_switched_off(part_a, shop):
    model, events = Scripted(), []
    comment, stats = agent_comment(part_a.to_dict(), shop, {"ANVILLE_AGENT": "off"}, events_into(events),
                                   model=model)
    assert comment is None and model.sent == [] and events == []
    assert stats == {"agent": "off"}


def test_missing_model_keys_fall_back_to_the_setup_count(part_a, shop):
    events = []
    comment, stats = agent_comment(part_a.to_dict(), shop, {}, events_into(events))
    assert comment is None and "MUSE_SPARK_OPEN" in stats["agent_error"]
    assert [(k, t) for k, t, _ in events] == [("error", "Agent failed; posting the setup count instead")]


def test_bad_facts_fall_back_to_the_setup_count(shop):
    comment, stats = agent_comment({"faces": "not a list"}, shop, {}, lambda *a, **k: None, model=Scripted())
    assert comment is None and "agent_error" in stats


def test_the_agents_comment_comes_back_with_a_record_of_every_call(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    model.host = "inference.baseten.co"
    verifier = Scripted(verdicts(True, True), name="verifier")
    events = []
    comment, stats = agent_comment(part_a.to_dict(), shop, {}, events_into(events), model=model, verifier=verifier)

    assert comment and chamfer in comment
    assert stats["agent_calls"] == 3 and stats["agent_fallbacks"] == []
    assert [(c["stage"], c["model"], c["host"]) for c in stats["agent_usage"]] == [
        ("plan", "scripted", "inference.baseten.co"), ("decide", "scripted", "inference.baseten.co"),
        ("verify", "verifier", None)]
    (summary,) = [d for k, t, d in events if t == "Agent finished"]
    assert summary["calls"] == stats["agent_usage"]  # the panel reads which host served each call


def test_a_model_knows_its_host():
    m = OpenAICompatModel("https://inference.baseten.co/v1/", "k", "meta-models/muse-spark-open-1026")
    assert m.host == "inference.baseten.co"


def test_the_jobs_deadline_reaches_every_model_call(part_a, shop):
    model = Scripted()
    comment, stats = agent_comment(part_a.to_dict(), shop, {}, lambda *a, **k: None, model=model,
                                   deadline=time.monotonic())
    assert model.sent == [] and stats["agent_calls"] == 0 and comment  # code-rendered, still posted


def test_the_job_hands_the_assessments_to_the_review(monkeypatch, part_a, shop, catalog_access):
    import anville.agent.job as job

    seen = {}

    def fake_review(facts, shop_, model, **k):
        seen.update(k)
        raise RuntimeError("stop after the call")

    monkeypatch.setattr(job, "run_review", fake_review)
    agent_comment(part_a.to_dict(), shop, {}, lambda *a, **k: None, model=Scripted(),
                  back_chamfer=[a.to_dict() for a in catalog_access])
    assert seen["back_chamfer"] == catalog_access


def test_the_job_imports_on_the_image_without_ocp():
    """The review job runs on Modal's small image, and its import sits outside the crash catch: an OCP
    import there would cost the designer the review. (analyze() alone runs on the OCP image.)"""
    import subprocess
    import sys

    code = "import sys; sys.modules['OCP'] = None; import anville.agent.job, anville.geometry"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]


def test_every_call_records_its_latency(part_a, shop, chamfer):
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route(), comment=sentences(chamfer)))
    _, stats = agent_comment(part_a.to_dict(), shop, {}, lambda *a, **k: None, model=model,
                             verifier=Scripted(verdicts(True, True), name="verifier"))
    assert all(isinstance(c["latency_s"], float) and c["latency_s"] >= 0 for c in stats["agent_usage"])


def test_events_are_flushed_when_the_stage_changes():
    """The panel animates the run live: each stage's events reach the store when the next stage starts,
    not in one burst after the agent returns (panel ask, 2026-10-29)."""
    log = []
    emit = flushing_at_stage_changes(lambda kind, title, **d: log.append(title), lambda: log.append("FLUSH"))
    emit("decision", "Issues chosen", stage="plan")
    emit("option", "Option scored", stage="plan")
    emit("error", "Agent failed")  # no stage: not a boundary
    emit("decision", "Decision made", stage="decide")
    emit("verify", "Sentence verified", stage="verify")
    # The event that opens a stage is written at once (Codex, PR #24): it comes before the stage's slow call.
    assert log == ["Issues chosen", "FLUSH", "Option scored", "Agent failed", "Decision made", "FLUSH",
                   "Sentence verified", "FLUSH"]


def _bought(cutter):
    """A shop_outcome row as Store.read_ledger returns it, for buying a catalog cutter."""
    from dataclasses import fields
    from types import SimpleNamespace

    from anville.shop import tool_acquired

    option = SimpleNamespace(kind="add_tool", key=f"add_tool/G3/+Z/BC45-{cutter.name}", params={
        "type": "back_chamfer", "owned": False,
        **{f.name: getattr(cutter, f.name) for f in fields(cutter) if f.name != "head_flat"}})
    entry = tool_acquired(option, review="review:part-a/v3", decision="comment:c42")
    return {"id": 1, "ledger": "shop_outcome", "scope": SHOP_SCOPE, "created_at": 1_793_000_000.0, **entry}


def test_the_review_shop_is_priced_and_owns_what_the_ledger_says_it_bought():
    from anville.geometry import BACK_CHAMFER_CATALOG
    from anville.shop import Shop

    shop, learned = review_shop([])
    assert isinstance(shop, Shop) and shop.costs and shop.back_chamfer_tools == [] and learned == {}
    assert catalog_for(shop) == list(BACK_CHAMFER_CATALOG)  # nothing owned: every cutter is a purchase

    cutter = BACK_CHAMFER_CATALOG[0]
    shop, learned = review_shop([_bought(cutter)])
    assert [t.name for t in shop.back_chamfer_tools] == [cutter.name]
    assert learned[cutter.name]["decision"] == "comment:c42"
    assert cutter not in catalog_for(shop)  # owned now: never offered as a purchase again


def test_a_malformed_ledger_row_costs_only_that_row():
    """Codex, PR #26 (P1): the store doesn't validate payloads, and a bad row must not stop the review."""
    from anville.geometry import BACK_CHAMFER_CATALOG

    good = _bought(BACK_CHAMFER_CATALOG[0])
    broken = {**good, "payload": {"kind": "tool_acquired", "type": "back_chamfer", "tool": {"name": "x"}}}
    shop, learned = review_shop([broken, good])
    assert [t.name for t in shop.back_chamfer_tools] == [BACK_CHAMFER_CATALOG[0].name] and list(learned) == ["910480"]


def test_part_b_says_it_plans_with_the_cutter_the_shop_bought(shop):
    """Shop lane's demo check of #26: Part B has no issues, so without this line the learning beat never
    reaches the comment."""
    from anville.geometry import BACK_CHAMFER_CATALOG, assess_back_chamfer, extract
    from tests.geometry.parts import PARTS, write_step

    import tempfile, pathlib
    step = write_step(PARTS["demo_bracket_b"](), pathlib.Path(tempfile.mkdtemp()) / "b.step")
    facts = extract(step)
    owned, learned = review_shop([_bought(BACK_CHAMFER_CATALOG[0])])
    learned = {name: {**src, "on": "2026-10-26"} for name, src in learned.items()}  # `on` arrives with #25
    access = assess_back_chamfer(step, facts, owned)
    comment, _ = agent_comment(facts.to_dict(), owned, {}, lambda *a, **k: None, model=Scripted(),
                               back_chamfer=[a.to_dict() for a in access], learned=learned)
    assert "1 setup" in comment
    assert comment.splitlines()[-1] == "It plans with the 910480 the shop bought on 2026-10-26."


# --- The setup sheet goes to the shop, and the shop's note comes back (demo gaps 2 and 3) ---------------------

def _decide_buy(part_a, shop, chamfer, catalog_access):
    from tests.agent.test_review import BUY

    model = Scripted(plan(*requests_for(chamfer)),
                     decide({"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}))
    return agent_comment(part_a.to_dict(), shop, {}, lambda *a, **k: None, model=model,
                         back_chamfer=[a.to_dict() for a in catalog_access])


def test_the_review_says_which_catalog_cutter_it_decided_to_buy(part_a, shop, chamfer, catalog_access):
    _, stats = _decide_buy(part_a, shop, chamfer, catalog_access)
    assert stats["buying"] == ["910480"]
    model = Scripted(plan(*requests_for(chamfer)), decide(propose_route()))
    _, stats = agent_comment(part_a.to_dict(), shop, {}, lambda *a, **k: None, model=model,
                             back_chamfer=[a.to_dict() for a in catalog_access])
    assert stats["buying"] == []


def test_the_sheet_plans_with_the_cutter_being_bought_and_marks_it_unowned(part_a, shop, catalog_access):
    """The traveler's 'arrived' box is what turns the purchase into a tool_acquired row."""
    from anville.agent.job import sheet_for

    access = [a.to_dict() for a in catalog_access]
    bought = sheet_for(part_a.to_dict(), shop, access, buying=["910480"])
    rows = [t for s in bought["setups"] for t in s["tools"]]
    assert len(bought["setups"]) == 1
    assert [t["owned"] for t in rows if t["name"] == "910480"] == [False]
    assert len(sheet_for(part_a.to_dict(), shop, access, buying=[])["setups"]) == 2


def test_a_shop_note_is_written_once_even_when_the_job_retries(part_a, shop, catalog_access):
    from anville.agent.job import shop_note_writes, sheet_for

    sheet = sheet_for(part_a.to_dict(), shop, [a.to_dict() for a in catalog_access], buying=["910480"])
    note = {"event": "shop.note", "note_id": "n1", "sheet_id": "s1", "outcome": "as_planned",
            "tools_arrived": ["910480"], "tags": [], "setups_run": 1, "note_text": ""}
    first = shop_note_writes(note, sheet, stored=[])
    assert [e["payload"]["kind"] for e in first] == ["shop_note", "tool_acquired"]
    stored = [{"id": i, **e} for i, e in enumerate(first, 1)]
    assert shop_note_writes(note, sheet, stored=stored) == []
    # and what it wrote is what the next review learns from
    owns, learned = review_shop(stored)
    assert "910480" in {t.name for t in owns.back_chamfer_tools} and "910480" in learned


def test_a_cutter_the_shop_already_owns_is_not_being_bought():
    from types import SimpleNamespace as NS

    from anville.agent.job import buying_from

    opt = lambda owned: NS(kind="add_tool", params={"type": "back_chamfer", "name": "910480", "owned": owned})  # noqa: E731
    pick = [NS(action="propose", option_key="k")]
    assert buying_from(NS(decisions=pick, options={"k": opt(False)})) == ["910480"]
    assert buying_from(NS(decisions=pick, options={"k": opt(True)})) == []
    assert buying_from(NS(decisions=[NS(action="quiet", option_key="k")], options={"k": opt(False)})) == []


# --- Codex, PR #40 ------------------------------------------------------------------------------------------

def _note(**kw):
    return {"event": "shop.note", "note_id": "n1", "sheet_id": "s1", "outcome": "as_planned",
            "tools_arrived": [], "tags": [], "setups_run": 1, "note_text": "", **kw}


def _bought_sheet(part_a, shop, catalog_access):
    from anville.agent.job import sheet_for

    return sheet_for(part_a.to_dict(), shop, [a.to_dict() for a in catalog_access], buying=["910480"])


def test_a_note_may_only_answer_a_sheet_on_its_own_document(part_a, shop, catalog_access):
    import pytest

    from anville.agent.job import shop_note_writes

    with pytest.raises(ValueError, match="document"):
        shop_note_writes(_note(), _bought_sheet(part_a, shop, catalog_access), stored=[],
                         sheet_document="doc-A", note_document="doc-B")


def test_a_replayed_note_id_cant_change_what_the_shop_reported(part_a, shop, catalog_access):
    """P1: a replay adding an arrival would have written tool_acquired under a note that reported none."""
    import pytest

    from anville.agent.job import shop_note_writes

    sheet = _bought_sheet(part_a, shop, catalog_access)
    stored = [{"id": i, **e} for i, e in enumerate(shop_note_writes(_note(), sheet, stored=[]), 1)]
    with pytest.raises(ValueError, match="already recorded"):
        shop_note_writes(_note(tools_arrived=["910480"]), sheet, stored=stored)


def test_a_job_that_died_mid_write_finishes_the_same_note(part_a, shop, catalog_access):
    from anville.agent.job import shop_note_writes

    sheet, note = _bought_sheet(part_a, shop, catalog_access), _note(tools_arrived=["910480"])
    first = shop_note_writes(note, sheet, stored=[])
    rest = shop_note_writes(note, sheet, stored=[{"id": 1, **first[0]}])  # only the shop_note row landed
    assert [e["payload"]["kind"] for e in rest] == ["tool_acquired"]


def test_a_cutter_ticked_twice_is_acquired_once(part_a, shop, catalog_access):
    from anville.agent.job import shop_note_writes

    writes = shop_note_writes(_note(tools_arrived=["910480", "910480"]), _bought_sheet(part_a, shop, catalog_access),
                              stored=[])
    assert [e["payload"]["kind"] for e in writes] == ["shop_note", "tool_acquired"]


def test_a_retried_review_reuses_its_sheet_id():
    from anville.agent.job import sheet_id_for

    assert sheet_id_for("version:v1") == sheet_id_for("version:v1") != sheet_id_for("version:v2")
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sheet_id_for("version:v1"))  # panel's sheet-id pattern
