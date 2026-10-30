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
