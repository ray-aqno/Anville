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

## Amendment 1: Fireworks reads the designer's reply (2026-10-31)

- **Status:** Accepted (Rayyan Aquino, 2026-10-31: "I'll accept those fireworks amendments and keys").
- **Lands in:** `docs/adrs/0011-fireworks-host-for-spark-open.md`, appended below the existing text. Nothing above it changes.

Labels: **verified** means checked today against main (`f1b5496`) or the live Fireworks page, with the source given. **Inference** means reasoned but not checked.

### Context
- **Failover alone never appears on camera.** Rayyan's directive (2026-10-31) is that every demo beat must be built and seen live before the Sunday noon freeze. Under ADR 0011's option A, Fireworks serves a call only when Baseten fails. So in a good take, Fireworks does nothing visible, and the only way to show it would be to stage an outage.
- **Fireworks serves the same model** (verified, fireworks.ai/models/fireworks/muse-spark-open, 2026-10-31):
  - `accounts/fireworks/models/muse-spark-open`, serverless on US-only capacity;
  - tool calling, image input (text output only);
  - a context of 1040k;
  - $1.25 / $0.15 / $4.25 per 1M tokens (input / cached / output);
  - the page doesn't mention reasoning effort.
- **Other Meta models on Fireworks** (verified, Fireworks blog and model pages):
  - Muse Glimmer 30B is listed, but its model page marks serverless "Not Supported" and being discontinued.
  - Llama 3.3 70B's serverless endpoints were removed on 2026-05-14.
  - I found no Muse Code page.
  So Muse Spark Open is the one model we use that Fireworks hosts.
- **The reply reader is its own model call** (verified):
  - `read_reply` (`anville/agent/reply.py`) makes one Muse Spark Open call per designer reply, through `OpenAICompatModel.from_env(env, "open")` (`job.py:196`).
  - Code accepts a rule only for a face group the review raised, and only with a quote copied from the reply.
  - On any exception the designer still gets an answer (`job.py:198–200`).
  - It runs at 1:05–1:12 in the demo script, in every take.
- **No live-model eval of `read_reply`** (verified): `scripts/evals/gate.py` puts reply rules in directly (`gate.py:140`) and never calls the reader.

### Decision
1. **Fireworks is the primary host for the reply reader;** Baseten is its failover. ADR 0011's Plan/Decide failover (Baseten primary, Fireworks backup) stays as accepted.
2. **`from_env(env, "reply")`** returns hosts in the order Fireworks, then Baseten, when `FIREWORKS_API_KEY` is set. Otherwise it falls back to today's `"open"` (Baseten only), so nothing breaks without the key.
   - `job.py:196` asks for `"reply"` instead of `"open"`.
   - The usage record carries the serving host, as §2 already specifies. So the trace and the panel chip read "Muse Spark Open · Fireworks" on the re-plan.
3. **Gate before it counts as working:** a `read_reply` probe on Fireworks, run only on Rayyan's go.
   - **Cases:** n=10 each of three replies: "Required: it's the weld clearance." (expect `keep`), "Not needed, it can go." (expect `may_change`, no `prefer`), and "Thanks, looks good." (expect no rules).
   - **Pass:** at least 9 of 10 per case, with every quote copied exactly.
   - **Cost (inference):** about 30 small calls, well under $1.
   - If it fails, the reader stays on Baseten and the demo drops Fireworks: no staged role.
4. **The trigger is the same as ADR 0011 §5:** no code until the key is in `.env`. The amendment itself can be accepted before then.

### Why the reply reader, not the alternatives
| Option | On camera in a good take? | Risk to the review |
|---|---|---|
| **Reply reader on Fireworks (chosen)** | Yes: every take, at 1:05 | Low: one bounded call; failover to Baseten; code checks every rule |
| ADR 0011 option A only (failover) | No, unless an outage is staged | None |
| Option B (Decide on Fireworks) | Yes | Higher: the main review's Decide depends on a host that hasn't passed the gate, and `reasoning_effort` isn't confirmed there (ADR 0011 §4.2) |
| Option D (vision on the render) | Yes | New work: the render isn't wired in, and it overlaps the rejected ADR 0005 |

### Consequences
- **Easier:** Fireworks does real work in every take, visible in the trace and the panel. It's also a distinct role, reading the designer, not just the same call on another host.
- **Harder:**
  - Two hosts in the re-plan path.
  - A reply read on Fireworks misses Baseten's prompt cache (negligible for one call).
  - The reader's behaviour on Fireworks has to pass its own probe, since the same weights on a different serving stack can parse tools differently (ADR 0011, Trade-offs).

### Probe result (2026-10-31, run on Rayyan's go: "you're going to test and all of our costs should be under $1")
- **Fireworks** (`accounts/fireworks/models/muse-spark-open`, the real `read_reply`; 31 calls):

  | Case | Result |
  |---|---|
  | `keep` ("Required: it's the weld clearance.") | **10/10** |
  | `may_change` ("Not needed, it can go.") | **10/10** |
  | no rules ("Thanks, looks good.") | **7/10**: 3 misses, each reading "looks good" as `keep` on G3 |

  - **Cost:** $0.126 at list price, from each response's usage (no cache discount assumed). The first call took 1,160 tokens in and 254 out (about $0.0025); calls took 1.2–17.8 s.
- **Baseten baseline,** same reader, the no-rules case only: **9/10**, with 1 identical miss.
- **Reading:** the over-read happens on both hosts. At n=10, 3/10 and 1/10 can't be told apart, so this is a weakness in the reader (a generic approval quoted as a decision), not a host difference. The bar of at least 9/10 per case is **not met** on Fireworks.
- **Next:** tighten the reader so a reply that only approves or thanks decides nothing (the prompt, the validator, or both; agent lane). Then re-run the no-rules case on both hosts: about $0.04 per 10 on Fireworks. Until the bar is met, the reader stays on Baseten (§3).

Re-run on 7c23e8a (ACKNOWLEDGEMENT rule): no rules 10/10; keep 3/3 and may_change 3/3 as a regression check. Gate passed; Fireworks is the reader's primary host.
