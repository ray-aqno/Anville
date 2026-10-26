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
