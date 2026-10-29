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

1. **Stages.** Code stages and model stages alternate:
   - Facts (code)
   - Triage (model)
   - Intent (code)
   - Options (model)
   - Decide (model + gate)
   - Render (code)

   **Each model stage makes one tool call, so a review takes 3 model calls** (the cap is 8: ADR 0003). If a call is invalid, the stage retries once, then falls back to **that stage's** fixed-script step. A stage's failure never sends the whole run to the fallback.
2. **Issues are computed, never invented.** Triage chooses among issues that code found in the `SetupPlan`:
   - `forces_setup`: a setup that exists for at most 2 face groups;
   - `unreachable`: faces with `no_tool` or `no_direction`.
3. **Intent tags are computed.** Each issue's face group is tagged *protected* (a stated reason: CAD feature name, drawing note, or a ledger constraint), *free* (a constraint allows changes), or *unknown*.
4. **The protected-face filter (ADR 0002 Amendment 1).** Part edits (`remove_feature`, `set_depth`, `set_min_radius`) on a protected group are rejected before `evaluate_edits` runs. The rejection and its reason are returned to the model as data.
5. **The value-of-information gate.** `ask` is offered for an issue only when:
   - its intent is *unknown*, **and**
   - the best plan-changing option differs between "part edits allowed" and "part edits excluded".

   Code runs the planner both ways. `propose` accepts only option keys that were offered in this run.
6. **The designer's comment is rendered by code** from the accepted decisions, the computed facts and the stated intent. Numbers are slots (`{{option.<key>.setups_after}}` and others) that code fills from tool results.
   - **The only model-written text in it is an accepted question.** That question has passed the gate, and it may contain no digit except through a slot.
   - **The model's reasoning for each decision goes to the trace,** where the panel shows it. It never goes into the comment.
   - *Changed during review (Greptile, PR #14):* a model-written Communicate stage could still argue for a change the gate had forbidden, in free text that no check could fully catch. Rendering the comment in code makes that impossible, and saves one model call.
7. **Model configuration.**
   - `max_tokens` is at least 4096.
   - `reasoning_effort` is sent only to the Meta Model API (`muse-spark-1.3`, `low`). Whether Baseten accepts it for Spark Open hasn't been tested.
   - `finish_reason: length` is logged as a budget failure and handled like any other invalid call.
8. **Trace.** Every stage emits ADR 0007 trace events with `detail.stage`, so the panel can show which stage the agent is in.

## Options considered

- **The free agent loop from ADR 0003:** rejected on the probe data. The guards there only acted after the fact.
- **A fixed pipeline:** kept as the per-stage fallback only.
- **A planner plus a critic model:** it doubles the calls and the latency against a 60 s budget.

## Consequences

- **Easier:**
  - each stage has its own tests;
  - the comment can't contradict the decisions, and a review is one call shorter (about 7 s of a 60 s budget);
  - the fallback costs nothing extra, because it reuses the same tools;
  - the panel gets stage names for free.
- **Harder:**
  - the comment reads more like a template. The model's voice in Onshape is its questions; its reasoning lives in the panel;
  - the `forces_setup` threshold (2 groups) is a judgment call, and it needs tests on more than the demo parts;
  - until the cost model lands (RFC 0002 §4), "best option" means fewest unreachable faces, then fewest setups, not dollars.
- **Revisit:**
  - when the cost model lands (the gate compares dollars);
  - when the multi-turn eval gate shows a stage failing its bar.
