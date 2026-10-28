# ADR 0003: The model drives the loop, tools compute every number

- **Status:** Proposed
- **Date:** 2026-10-26
- **Depends on:** ADR 0002

## Context

ADR 0002 defines a deterministic planner and an edit catalog. Something has to decide which candidates to explore, when to ask the designer about intent, when to stay quiet, and when a shop-side investment is worth proposing. Two options: a fixed pipeline where the model only writes prose, or a model that orchestrates through tools. Either way, no number may come from the model (locked decision 3).

## Decision

**Muse Spark Open orchestrates the review loop through tool calls. Code computes every number.**

**Tool layer.** Plain functions with typed JSON schemas:
- `plan(world)`
- `evaluate_edits([edit])`, batched; edits must come from the catalog
- `query_ledger(filter)`
- `ask_designer(question)`
- `propose_to_shop(edit)`
- `execute(option_id)`
- `stay_quiet(reason)`
- `log_outcome(plan_id, actual)`

**Guards:**
- At most 8 tool calls per review.
- **Numbers in comments are slots** (`{{r3.delta_cost}}`) that code fills from tool results. A slot that matches no tool result rejects the comment, with one regeneration allowed.
- If the model makes malformed tool calls twice, or is unavailable, the run falls back to a **fixed-order script that calls the same tools**. If even that fails, Anville posts a code-only comment.

**Trust boundary:**
- The webhook URL carries a secret path token, and the `documentId` must be on an allowlist.
- **Events Anville itself caused** (its own `versionId` or `commentId` values, or events by the API-key user) are dropped, so Anville can't trigger itself.
- `execute` accepts only an option ID that Anville offered in that thread.
- Tools are scoped to the document that triggered the run. Comment text reaches the model as quoted data.

**Concurrency:**
- One loop per document at a time.
- A newer version marks the in-flight run stale.
- Webhooks are idempotent: `versionId` keys new-version events, and `commentId` keys replies.

**Provenance.** Every tool call is logged with run ID, model ID, prompt version, inputs, outputs, and outcome. The same log drives a live trace view.

## Options considered

1. **Fixed pipeline, model writes prose.** Predictable, but the model only narrates and has no autonomy.
2. **Model orchestrates through tools (chosen).** Real decisions sit with the model, and the facts sit with code.
3. **The model computes estimates itself.** Rejected: it violates "compute, don't guess."

## Trade-offs

- It depends on reliable tool calling from Muse Spark Open, which is **not yet verified**. A probe (2 tools, JSON schema, 10 trials) decides it. Below 9 of 10 valid calls, the fixed script becomes the primary path.
- Model decisions inside the loop are harder to debug than a fixed pipeline. The provenance log and the trace view compensate.
- More model round trips put pressure on the target of about 60 seconds from save to comment, mitigated by batched evaluation and the call cap.

## Consequences

- **Easier:** the fixed script and the agent share one tested tool layer, so the fallback costs nothing extra. Every claim in a comment can be traced to a tool result.
- **Harder:** prompt and tool-schema changes need regression runs against recorded fixtures with a scripted fake model.
- **Revisit:** if the tool-calling probe fails, or if 8 calls aren't enough for parts with several constraints.

## Amendment 1 (Proposed, 2026-10-27): probe result and the agent eval gate

**Probe result (measured 2026-10-27, `scripts/probe_tool_calls.py`).** Setup: 2 tools (`evaluate_edits`, `ask_designer`) with JSON schemas, 20 synthetic review situations, one request each. A call is valid when there is exactly one tool call, its name is known, and its arguments parse and pass the schema.

| Model | Setting | Valid | 95% lower bound | Expected tool | Grounded | p50 / max latency |
|---|---|---|---|---|---|---|
| `meta-models/muse-spark-open-1026` | `max_tokens` 4096 | **20/20** | 84% | 16/20 | 20/20 | 4.4 s / 8.6 s |
| `muse-spark-1.3` | `max_tokens` 4096, default effort | 15/20 | 53% | 11/20 | 15/15 | 6.9 s / 16.3 s |
| `muse-spark-1.3` | `reasoning_effort: low` | **20/20** | 84% | 14/20 | 20/20 | 4.4 s / 7.2 s |
| `muse-spark-1.3` | `reasoning_effort: medium` | **20/20** | 84% | 14/20 | 20/20 | 6.0 s / 12.4 s |

The model-driven loop stays the primary path. The bar is raised to **at least 18/20 (90%)** at n = 20, because 9/10 has a 95% lower bound of only about 60%.

**What we learned:**
1. **`max_tokens` covers reasoning as well as output** (Meta Model API docs, "Reasoning"). With 1024, Spark Open managed 3/10. Every failure had `content: null`, and the same cases needed 1,178–2,304 tokens with a larger cap. All 5 failures of `muse-spark-1.3` at default effort were `finish_reason: length` at 4096. **Rule:** `max_tokens` is at least 4096 for every agent call, and `finish_reason: length` is logged as a budget failure, not a model failure. `muse-spark-1.3` runs with `reasoning_effort: low`.
2. **The models disagree on when to ask the designer.** Spark Open tends to act: it skipped 3 expected questions. `muse-spark-1.3` tends to ask: it asked on 4 cases labeled "evaluate". Both proposed edits that contradicted stated intent, such as changing the depth of a fit dimension or rounding a corner marked as a seal. **Code must reject edits that violate a recorded constraint before the planner scores them.** That can't be left to the prompt.
3. **The models invent edit values** (for example, depths of 32 mm and 24 mm). Under locked decision 3, the agent's `evaluate_edits` schema should take an edit *kind* and a *face group*. The planner generates candidate values from the shop's tool library and the geometry.
4. **Latency:** about 4–5 s per call, against a cap of 8 calls, takes most of the 60 s budget. The loop should batch scoring into one `evaluate_edits` call per review step.

**The agent eval gate (built alongside the agent loop, then run on every prompt, schema, model, or effort change, and before recording the demo):**
1. **Single-turn validity:** this probe, rebuilt to use the real tool schemas from the agent module, at n = 20. The bar is 18/20 for each model at its configured effort.
2. **Multi-turn scenarios:** the real model drives the real loop against a **scripted fake tool layer**, with fixtures that return fixed planner and ledger results. There is one scenario per demo beat (first review, designer reply → re-plan, fix branch, shop note, second part informed by the ledger), plus adversarial ones (intent conflicts, an unreachable feature, nothing to say). Each scenario runs k = 5 times. Checks:
   - every call is valid;
   - at most 8 calls, and the run ends with a comment or `stay_quiet`;
   - every number in the comment is a slot that resolves to a tool result;
   - no edit that violates a constraint reaches `execute`;
   - `execute` only uses an option ID that was offered;
   - after a designer reply, the next plan respects the captured constraint.
   - **Pass:** all checks hold in at least 4 of 5 runs of every scenario, and in 5 of 5 for the demo-beat scenarios.
3. **Results are committed** as summary JSON (pass rates, p50/max latency, and token use per scenario). They are evidence for the writeup, the same way the probe's results are.

The gate runs locally, not in CI: it needs API keys and spends credits. It costs cents per run.

**Relabel (2026-10-28, ruled by Rayyan).** Every model run disagreed with three labels in the same direction. The RFC 0001 gate shows the labels were wrong:
- **Cases 2 and 4 → score options.** The intent is stated (a fit dimension; a corner that seals), so the face is *protected* and there's nothing to ask. The right move is a shop-side fix.
- **Case 9 → ask.** No tool fixes "no direction", so only a design change can, and its intent is unknown.

The probe now also grades **respects intent**: no part edit (`set_min_radius`, `set_depth`) on a protected face group. Re-scoring the saved n = 20 runs offline:

| Model | Expected tool | Respects intent |
|---|---|---|
| Spark Open `-1026` | 19/20 | **18/20**: it changed G2's fit-dimension depth and rounded G5's sealing corner |
| `muse-spark-1.3` (low and medium) | 17/20: it asked on 3 shop-only cases | **20/20** |

So Spark Open's real weakness is not asking versus acting; it ignores stated intent when it proposes part edits. RFC 0001's protected-face filter removes that in code, before the planner scores anything.
