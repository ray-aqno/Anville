"""The agent inside the deployed review job (S3b): never costs the designer a review, and leaves a
per-call record (stage, model, host, tokens) for the trace."""

import time

from anville.agent.job import agent_comment
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
