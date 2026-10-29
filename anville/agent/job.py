"""The agent inside the deployed review job (ADR 0009). Kept free of Modal so it runs in tests.

The job always has a fallback: the plain setup-count line, which code renders from the SetupPlan.
So the agent returns None whenever it is switched off or breaks, and a review is never lost to an
agent bug.
"""

from __future__ import annotations

from typing import Mapping

from anville.agent.model import ChatModel, OpenAICompatModel
from anville.agent.review import Emit, run_review
from anville.geometry import PartFacts, ShopProfile


def agent_comment(facts: dict, shop: ShopProfile, env: Mapping[str, str], emit: Emit, *,
                  model: ChatModel | None = None, verifier: ChatModel | None = None) -> tuple[str | None, dict]:
    """The agent's comment and its run stats for the job's timing log.

    `ANVILLE_AGENT=off` skips it. Models come from `env` unless passed in; the verifier
    (muse-spark-1.3 on the Meta Model API) is used only when its key is set.
    """
    if env.get("ANVILLE_AGENT", "on") != "on":
        return None, {"agent": "off"}
    try:
        model = model or OpenAICompatModel.from_env(env, "open")
        if verifier is None and env.get("MODEL_API_KEY"):
            verifier = OpenAICompatModel.from_env(env, "spark")
        r = run_review(PartFacts.from_dict(facts), shop, model, verifier=verifier, emit=emit)
    except Exception as e:  # an agent bug must not cost the designer the review
        error = repr(e)[:300]
        emit("error", "Agent failed; posting the setup count instead", error=error)
        return None, {"agent_error": error}
    # One record per call (stage, model, host, tokens): the panel shows which host served what.
    emit("verify", "Agent finished", stage="publish", calls=r.usage, fallbacks=r.fallbacks)
    return r.comment or None, {"agent_calls": r.model_calls, "agent_fallbacks": r.fallbacks,
                               "agent_usage": r.usage}
