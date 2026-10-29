# ADR 0009: The agent loop is a stage graph with a gated action space

- **Status:** Proposed
- **Date:** 2026-10-28
- **Depends on:** RFC 0001 (Part A, option A3); ADR 0002 and Amendment 1 (`evaluate_edits`, `remove_feature`); ADR 0003 and Amendment 1 (tool calling, probe results, eval gate); ADR 0008 (`PartFacts`, `SetupPlan`)
- **Feeds:** ADR 0007 (trace events and stages in the panel)

## Context

The probe (ADR 0003 Amendment 1, with the relabel) showed that both Meta models produce valid tool calls, but their judgment drifts:
- **Spark Open** acts without asking, and made part edits on faces with stated intent in 2 of 20 cases.
- **`muse-spark-1.3`** asks when it should act.
- Both run past a 1024-token cap while reasoning.

RFC 0001 proposed the fix: the harness owns *what is allowed*, and the model owns *what is worth doing*. This ADR records the first implementation, `anville/agent/`, so the agent loop can be built.

## Decision

1. **The graph.** Code and model steps alternate; **a review is 3 model calls** (the cap is 8: ADR 0003):

   ```
   Facts + Intent (code) → Plan (model) → Decide + Write (model + gate) → Check (code) → Verify (model #2) → Publish
   ```

   An invalid call is retried once, then replaced by **that stage's** fixed-script step. A stage's failure never sends the whole review to the fallback.
2. **Issues are computed, never invented.** Plan chooses among issues that code found in the `SetupPlan` (`forces_setup`: a setup that exists for at most 2 face groups; `unreachable`: faces with `no_tool` or `no_direction`), and names the edit kinds to score. **Plan sees geometry only, not intent.** Live, planning that saw "protected, ask not allowed" dropped the main issue in 3 of 6 stated-intent runs; with geometry only it raised it 6 of 6.
3. **Intent tags are computed** for every group with something on record: *protected* (a stated reason from a CAD feature name, a drawing note, or a ledger constraint), *free*, or *unknown*. A ledger constraint outranks a stated reason.
4. **The protected-face filter (ADR 0002 Amendment 1).** Part edits that touch a protected group, including concave neighbours of the named group, are rejected before `evaluate_edits` runs. The rejection is returned to the model, and shown in the trace.
5. **The value-of-information gate.** `ask` is allowed for an issue only when its intent is *unknown* and the best plan-changing option differs between part edits allowed and part edits excluded (ignoring part edits that would touch a protected neighbour). `propose` accepts only option keys offered in this run.
6. **The comment is part of the graph.**
   - **Decide + Write** writes the comment as sentences, each citing the nodes it rests on (issues, intents, options, its own decisions) and declaring a role (`context`, `action`, `reason`).
   - **Check (code)** rejects a sentence that: cites unknown nodes; has a number outside a slot; asks a question; is an action without exactly one propose/quiet decision; names an option or edit kind no cited decision accepted; or puts node IDs or slot names in the words.
   - **Verify (a second model, `muse-spark-1.3`)** rejects a sentence that says more than its cited nodes, or suggests anything beyond the accepted decisions.
   - **Publish:** verified sentences go out in the model's order. Any decision not stated by a verified action sentence gets a line rendered by code, and accepted questions are always placed by code. Without a verifier, or if it fails, no model prose is published.
   - *Changed during review (Greptile, PR #14).* An earlier version published unchecked model prose, and a later one removed prose entirely. The graph keeps the model's voice and makes every published sentence traceable to the fact it rests on.
7. **Model configuration.**
   - `max_tokens` is at least 4096 everywhere (6144 for Plan and Decide).
   - `reasoning_effort` is set per stage. **Baseten honours it for Spark Open** (verified 2026-10-29: `low` cut completion tokens 4–6×). Plan runs at the default (at `low` it once dropped the main issue); Decide + Write runs at `low` (correct in every live run). The verifier runs `muse-spark-1.3` at `low`.
   - `finish_reason: length` is logged as a budget failure and handled like any other invalid call.
   - **Prompt order is cache-friendly:** one system prompt and a shared facts prefix, with stage-specific text last. Baseten reported up to 1,152 of 1,166 prompt tokens cached.
   - Usage is recorded for every call.
8. **Trace.** Every step emits ADR 0007 trace events with `detail.stage`, including the model's reasoning for each decision and each sentence's check and verify verdict. The panel shows which stage the agent is in, and why.

**Measured (live, Muse Spark Open + `muse-spark-1.3`, demo Part A, 2026-10-29):** 3 calls, no fallbacks, 12–21 s per review. The stated-intent beat proposed the shop-side fix 6 of 6 times, with the protected removal rejected by the filter.

## Options considered

- **The free agent loop from ADR 0003:** rejected on the probe data. The guards there only acted after the fact.
- **A fixed pipeline:** kept as the per-stage fallback only.
- **A planner plus a critic model:** it doubles the calls and the latency against a 60 s budget.

## Consequences

- **Easier:**
  - each stage has its own tests;
  - every published sentence is traceable to the nodes it cites, and the comment can't state an action the gate didn't accept;
  - a review is 3 calls and about 12–21 s, down from 25–35 s;
  - the fallback costs nothing extra, because it reuses the same tools;
  - the panel gets stage names for free.
- **Harder:**
  - two models, so two hosts to keep healthy. Without the verifier, the comment falls back to code-rendered lines;
  - the check and verifier can reject good sentences. The cost is a rendered line in their place, never a wrong one;
  - the `forces_setup` threshold (2 groups) is a judgment call, and it needs tests on more than the demo parts;
  - until the cost model lands (RFC 0002 §4), "best option" means fewest unreachable faces, then fewest setups, not dollars.
- **Revisit:**
  - when the cost model lands (the gate compares dollars);
  - when the multi-turn eval gate shows a stage failing its bar.
