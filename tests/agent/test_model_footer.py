"""The credits line under every review: which models did the work, on which hosts, from the job's call records."""

from anville.agent.model import credits_line

OPEN, VERIFIER = "meta-models/muse-spark-open-1026", "muse-spark-1.3"
BASETEN, META = "inference.baseten.co", "api.meta.ai"


def call(stage, model=OPEN, host=BASETEN, latency_s=6.0, **extra):
    return {"stage": stage, "model": model, "host": host, "latency_s": latency_s, **extra}


REVIEW = [call("plan", latency_s=8.2), call("decide", latency_s=5.1), call("verify", VERIFIER, META, 6.4)]


def test_a_review_names_each_model_its_host_and_what_it_did():
    assert credits_line(REVIEW) == ("Reviewed by Muse Spark Open (Baseten) · checked by Muse Spark 1.3 (Meta API) · "
                                    "3 model calls · 20 s")


def test_every_call_counts_including_retries_and_failed_ones():
    retried = [call("plan"), call("plan", error="timeout"), *REVIEW[1:]]
    assert "· 4 model calls ·" in credits_line(retried)


def test_a_stage_that_fell_back_to_code_says_so():
    assert credits_line(REVIEW, fallbacks=["plan"]).endswith("· plan fell back to code")


def test_a_failover_host_is_named_beside_the_primary():
    """ADR 0011: when Fireworks serves a call, the line shows it."""
    fireworks = call("decide", "accounts/fireworks/models/muse-spark-open", "api.fireworks.ai")
    failed_over = [REVIEW[0], fireworks, REVIEW[2]]
    assert credits_line(failed_over).startswith("Reviewed by Muse Spark Open (Baseten, Fireworks) · ")


def test_a_reply_turn_starts_with_the_reader():
    line = credits_line([call("read_reply", latency_s=3.0), *REVIEW])
    assert line.startswith("Reply read by Muse Spark Open (Baseten) · reviewed by Muse Spark Open (Baseten) · ")


def test_new_stages_extend_the_line_and_an_unknown_model_shows_its_own_id():
    """fix_branch (Muse Code) and anchor (SAM 3.1) records land later; nothing is named that didn't run."""
    line = credits_line([*REVIEW, call("fix_branch", "muse-code-preview", "api.meta.ai"),
                         call("anchor", "facebook/sam3.1", "anville--sam.modal.run")])
    assert "· fix drafted by Muse Code (Meta API) · flags anchored by facebook/sam3.1 (Modal) ·" in line
    assert "· 5 model calls ·" in line


def test_no_calls_no_line():
    assert credits_line([]) == ""


def test_the_reply_turn_hands_back_the_readers_call_records(part_a, shop):
    """A re-plan's line starts with the reader, so read_reply_turn must return its records."""
    from anville.agent.job import read_reply_turn
    from tests.agent.test_reply import REPLY, keep, rules
    from tests.agent.test_review import Scripted

    found, groups, _, (calls, _) = read_reply_turn(part_a.to_dict(), shop, {}, lambda *a, **k: None, REPLY,
                                              model=Scripted(rules(keep())))
    assert found and groups == ["G3"]
    assert [c["stage"] for c in calls] == ["read_reply"]


def test_the_line_goes_last_and_only_under_model_work():
    from anville.agent.job import with_credits

    assert with_credits("Review.\nSetup sheet for the shop: link", REVIEW).splitlines()[-1].startswith("Reviewed by")
    plain = "Anville reviewed version 1234: 2 setup(s)."
    assert with_credits(plain, []) == plain


def test_a_reply_read_that_fell_back_says_so(part_a, shop):
    """Codex, P2: both reader attempts rejected, so its calls are credited but marked as a fallback."""
    from anville.agent.job import read_reply_turn
    from tests.agent.test_reply import REPLY, keep, rules
    from tests.agent.test_review import Scripted

    bad = rules(keep("words the designer never wrote"))
    found, _, _, (calls, fallbacks) = read_reply_turn(part_a.to_dict(), shop, {}, lambda *a, **k: None, REPLY,
                                                      model=Scripted(bad, bad))
    assert found is None and len(calls) == 2
    assert credits_line(calls, fallbacks).endswith("· read_reply fell back to code")
