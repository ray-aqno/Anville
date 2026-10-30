# ADR 0011: Fireworks as a second host for Muse Spark Open

- **Status:** Accepted (Rayyan Aquino, 2026-10-30)
- **Date:** 2026-10-30
- **Supersedes:** locked decision 6 in the project brief, in part ("Fireworks: nothing until signup works"). Signup now works.
- **Depends on:** ADR 0004 (model roles and hosts), ADR 0009 (stage graph, per-call `host` in the trace)
- **Numbering:** 0010 is the shop lane's cost-ranked any-intent fixes.

## Context

- **One host today.** Muse Spark Open runs Plan and Decide on Baseten, with the hackathon key scoped to `meta-models/muse-spark-open-1026` (ADR 0004). When Baseten fails, `OpenAICompatModel` raises `ModelUnavailable`, and the stage falls back to its fixed script. The review still posts, but the model's own decision is lost.
- **Survival matters.** The eval gate (PR #31) scores survival: how often the model's decision reaches the comment. On PR #23 it was 98 of 100, and both misses were invalid answers, not host failures. So the risk is an outage, not today's rate.
- **Fireworks serves the same model.** Checked 2026-10-30 on fireworks.ai/models/fireworks/muse-spark-open:
  - model ID `accounts/fireworks/models/muse-spark-open`, serverless on US-only capacity;
  - tool calling, plus "native image understanding in a single checkpoint";
  - a context of 1040k tokens;
  - $1.25 input, $0.15 cached input, $4.25 output per 1M tokens.
- **The API (docs.fireworks.ai, OpenAI compatibility):**
  - OpenAI-compatible at `https://api.fireworks.ai/inference/v1`, with a bearer key;
  - one documented difference: when prompt plus `max_tokens` exceeds the context, Fireworks lowers `max_tokens` by default, where OpenAI errors. That can't happen at Anville's prompt sizes.
- **Not verified:**
  - Whether Fireworks honours `reasoning_effort` for this model. Its reasoning guide documents the parameter (`"low"`, `"medium"`, `"high"`) for reasoning models in general but doesn't name this one, and says nothing about unsupported models. On Baseten, `low` cut Decide's completion tokens 4–6× (ADR 0009 §7), and Decide depends on it.
  - Whether the same weights behave the same behind a different serving stack. Templates and tool-call parsing differ by host.
- **Rayyan's rules:**
  - every sponsor gets a real role;
  - the demo shows as much visible model work as possible (memory, 2026-10-29).
- **Credits:** Rayyan has submitted the credit form. They are not confirmed, and no key is in `.env` yet.

## Decision

1. **Fireworks becomes the failover host for Muse Spark Open.** Baseten stays primary.
   - When the primary raises `ModelUnavailable` (an HTTP error, a timeout, or a malformed body), the same request is sent once to Fireworks, within the call's remaining time budget.
   - **An invalid answer does not fail over.** The weights are the same, so another host won't fix a judgment error. Retrying is the stage's job (ADR 0009 §1).
2. **The failover lives in `anville/agent/model.py`, and `run_review` doesn't change** (lane contract 4).
   - `OpenAICompatModel.from_env(env, "open")` returns a model that tries its hosts in order. It adds Fireworks only when `FIREWORKS_API_KEY` is set.
   - The reply's usage record carries the `host` and `model` that actually served the call. `_Runner` merges the usage dict over its defaults, so the trace and the panel's host chips show Fireworks on the calls it served, with no change to `review.py`.
3. **Configuration:**
   - `FIREWORKS_API_KEY` (Rayyan adds it to `.env`);
   - `FIREWORKS_MODEL_ID`, defaulting to `accounts/fireworks/models/muse-spark-open`;
   - the base URL is a constant, like `META_API`.
4. **Probes and gates before the failover counts as working.** Each costs cents to about $2, and runs only on Rayyan's go:
   1. **Validity:** single-turn tool-call validity on Fireworks, using the real Plan and Decide tool schemas.
   2. **`reasoning_effort`:** Decide's completion tokens at `low` and at default, n=5 each.
      - If `low` isn't honoured, Decide on Fireworks runs at default effort. That's slower (Baseten's default Decide reached about 5,000 completion tokens) but still correct.
      - In that case the ADR records it, and `STAGE_MAX_TOKENS` must still cover a default-effort Decide.
   3. **The eval gate with Fireworks forced as primary,** n=10 per scenario (about $1.80). Fireworks counts as a working failover only if it passes the same bars as Baseten.
5. **No failover code until the credits land and the key is in `.env`.** This ADR can be accepted before then.

## Options considered

| Option | Fireworks' role | Cost | Visible in the demo? |
|---|---|---|---|
| **A. Failover only (chosen)** | Backup when Baseten fails | Lowest: called only on failure | Only when Baseten fails, or when failover is shown on purpose |
| **B. Active-active, split by stage** | Plan on Baseten and Decide on Fireworks, each the other's failover | Both hosts in every review | Yes: both sponsors' chips in every trace |
| **C. Fireworks primary** | Main host; Baseten is the backup | Fireworks credits, not the hackathon's | Yes, but the Baseten hackathon deployment goes unused |
| **D. Fireworks for the vision probe only** | Image input of rendered views (kickoff goal 3) | Probe only | Only if the probe earns a role |

**Why A first:**
- It is the smallest change that gives Fireworks a real job.
- It needs no decision about which host is better before there's data.
- It keeps the hackathon's Baseten deployment as the primary.

**B is the strongest demo option**, and it's a standard reliability pattern, not decoration. But it puts a host that hasn't passed the gate into every review, so it should follow probe 3, not precede it.

**D stays open** as kickoff goal 3. It's independent of this decision.

## Trade-offs

- **Two hosts, two serving stacks.** The same weights can still behave differently: tool-call parsing and reasoning budgets differ by host. Probe 3 is what makes the failover trustworthy, not the model ID.
- **Time.** A Baseten timeout spends the call's budget, which leaves Fireworks little or no time. Failover helps most against fast failures (5xx, 403, connection refused), and least against hangs.
- **Prompt cache.** A failed-over call misses Baseten's prompt cache. It costs a little more on that one call.
- **Cost:** Fireworks credits are spent only on failover under A. They're not confirmed yet.

## Consequences

- **Easier:**
  - A Baseten outage no longer costs the model's own decision.
  - Fireworks has a real role in the architecture, with the trace showing exactly which calls it served.
  - Moving to B later is a configuration change.
- **Harder:**
  - A second host to keep healthy, and a second key.
  - The eval gate needs a forced-host mode, so each host can be measured.
  - Any prompt or schema change is gated on both hosts before relying on failover.
- **Revisit:**
  - **After probe 3:** if Fireworks passes at parity, consider B for the demo.
  - **If `reasoning_effort` is ignored on Fireworks:** record it, and recheck Decide's token cap.
  - **If the credits don't land:** this ADR stays Proposed, and §3.6 stands.
