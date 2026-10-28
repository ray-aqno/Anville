# RFC 0001: Orchestration within a manufacturing standard, and domain knowledge without fine-tuning

- **Status:** Draft
- **Date:** 2026-10-27
- **Touches:** ADR 0002 (planner), ADR 0003 (agent loop, Amendment 1), ADR 0004 (model selection), ADR 0006 (store), locked decision 4 ("the model reasons, the harness learns")

## Problem

ADR 0003 gives Muse Spark Open the review loop and has it act through tools. The first measurements (ADR 0003 Amendment 1) show two failure modes that the ADR's after-the-fact guards don't address:

1. **Judgment drift on when to act and when to ask.** The two models fail in opposite directions:
   - Spark Open acts when the designer should be asked: it skipped 3 expected questions out of 20.
   - `muse-spark-1.3` asks when it should act: it asked on 4 cases that only needed scoring, such as "the tap is too short".
   - Both proposed edits that contradict stated intent: changing the depth of a fit dimension, and rounding a corner marked as a seal.
   - Both made up edit values (for example, depths of 32 mm and 24 mm).
2. **The reasoning budget runs out before the model acts.** `max_tokens` covers reasoning as well as output.
   - At a cap of 1024, Spark Open managed 3/10.
   - At default effort, `muse-spark-1.3` spent about 3,000–4,100+ tokens per decision. The same case landed on either side of a 4096 cap from one run to the next.
   - **These are long deliberations, not loops:** both cases that had been cut off finished at about 3,450 reasoning tokens when given a 16k cap (verified, n = 2; the API returns no reasoning text, so repetition can't be inspected directly).
   - `reasoning_effort: low` gave 20/20 valid calls.

That leaves two design questions:
- **A.** How should the loop be structured so the model's autonomy is real, but its actions stay inside a manufacturing standard?
- **B.** How does the model get specialized manufacturing judgment **without fine-tuning**? Locked decision 4 requires this, and it's the substance of YC's "Own Your Intelligence" RFS. That RFS names the hard part as turning "workflows, expertise, and production failures into evals and training data that carry over to new base models".

## Constraints

- **Code computes every number** (locked decision 3). **Learning lives in records, not weights** (locked decision 4). Meta models do all AI work at runtime.
- **Budgets:**
  - at most 8 model calls per review;
  - about 60 s from save to comment;
  - about 4–5 s per model call at low effort (measured).
- **Judging:** Best Agentic scores autonomy, effectiveness and usefulness. A pipeline in disguise scores poorly on autonomy.
- **Resources:** solo, about 8 days left, no new services.

## Part A: Orchestration

### Options

| Option | Autonomy | Addresses drift? | Addresses budget? | Cost |
|---|---|---|---|---|
| **A1. Free agent loop** (ADR 0003 as written) | Highest | No: guards only catch problems after the fact | No: one budget for every decision | Lowest |
| **A2. Fixed pipeline, model writes prose** | None | Yes, by removing judgment | Yes | Low. It's already the fallback |
| **A3. Stage graph (standard work), gated actions, budget per stage** | High where judgment matters | Yes: the allowed actions depend on computed facts | Yes: effort per stage, fallback per stage | Medium |
| **A4. Planner plus critic model** | High | Partly: the critic shares the planner's blind spots unless it has domain knowledge (Part B) | No: doubles the calls | Doubles the latency |

### Recommendation: A3

The harness owns **what is allowed**. The model owns **what is worth doing**.

```
 Facts ──▶ Triage ──▶ Intent ──▶ Options ⇄ Score ──▶ Decide ──▶ Communicate
 (code)    (model)    (code)     (model)  (code)    (model     (model)
                                    └─ loop back while       + gate)
                                       nothing beats baseline
```

| Stage | Who | The model may | Effort | Exit check (code) |
|---|---|---|---|---|
| Facts | code | nothing: geometry, shop and ledger produce `PartFacts` | none | complete |
| Triage | model | choose which *computed* violations to pursue; drop others with a reason. It cannot invent violations | low | every violation is chosen or dropped |
| Intent | code | nothing: each issue is tagged *protected*, *free* or *unknown* from the ledger and evidence (ADR 0005) | none | none |
| Options | model | chain `evaluate_edits` calls choosing edit **kinds** on face groups. Protected faces aren't offered; the planner generates the values | low | something beats the baseline, or a quiet reason is recorded |
| Decide | model, plus the gate | choose among the actions code allows | low | the action was allowed |
| Communicate | model | write the comment; numbers are slots | medium | every slot resolves |

**The value-of-information gate.** `ask_designer` is offered for an issue only when:
- its intent is *unknown*, **and**
- the planner's best option differs depending on whether the face is protected or free.

The planner runs twice on hypothetical worlds (ADR 0002 already scores copies of the world). If the best plan is the same either way, the question changes nothing, and it isn't offered. This turns the ask-or-act disagreement into a computed fact. The model still chooses which allowed question matters most and writes it.

**Budget policy:**
- `max_tokens` is at least 4096 everywhere, and effort is set per stage.
- `finish_reason: length` counts as a budget failure. The stage retries once at lower effort, then falls back to **that stage's** fixed-script step. The whole run never falls back for one stage's failure.

**Typed stage outputs** go into the ADR 0006 store. That makes runs resumable. It also feeds the panel trace: ADR 0007 shows which stage the agent is in and why.

**Where autonomy lives:**

| The model decides | Code decides |
|---|---|
| which issues are worth pursuing | which issues exist |
| which edit kinds to try, and in what order | the values of edits, and their $ and setups |
| when to loop back to Options | whether an option beats the baseline |
| which allowed action to take, including staying quiet | which actions are allowed (the gate, the ledger constraints) |
| what to tell the designer and the shop | every number in the message |

## Part B: Domain knowledge without fine-tuning

Knowledge lives in five layers outside the weights. Each layer has an owner, a way into context, and a way to learn.

| Layer | What it holds | How it reaches the model | How it learns |
|---|---|---|---|
| **1. Physics** (computed) | reach, tool fit, setups, cost | tool results; the model never needs to know it | code changes, tested |
| **2. Standard** (codified) | the stage graph, gates, edit catalog, DFM rule tables (corner radius vs tool, L:D, minimum wall, thread depth), each with a source and scope | the action menu and gate results, not prose | reviewed changes (ADR or PR) |
| **3. Playbook** ("system prompt learning") | short heuristics per stage, versioned, with provenance and scope (global / team / shop). Example: "on a fit dimension, try shop-side options before asking" | the top entries for the current stage and situation tags, at most about 5 per stage | the outer loop (Part C), plus human confirmation |
| **4. Episodes** (ledger) | the designer's constraints, shop outcomes, accepted runs | matching ledger records, plus 1–2 worked examples from accepted runs with the same tags | every loop that closes (ADR 0002 / 0006) |
| **5. Evals** | scenarios, labels, pass bars | not shown to the model; used only to judge changes | Rayyan owns the labels |

Layer 3 follows Karpathy's proposed "system prompt learning": strategies written down and refined, as opposed to weights updated. (Source: secondhand coverage of his post; the original wasn't retrieved. Label: prior plus secondary.)

**Layers 4 and 5 together are the asset "Own Your Intelligence" describes.** They're portable: on a new base model, re-run layer 5 and keep whichever model clears the bar (ADR 0004). They could also become fine-tuning data later, but nothing here depends on that.

## Part C: The Karpathy loop (outer loop over layers 2–4)

This is a keep-or-revert ratchet, in the style of Karpathy's autoresearch. (Source: secondary coverage. The pattern: edit, run a fixed-budget experiment, keep the change only if the metric improves, use git as memory.)

1. **Propose** one change: a playbook entry, a stage's effort or budget, a worked example, or a rule-table entry. Proposals come from three sources:
   - eval failures;
   - designer replies that overrode Anville;
   - logged shop outcomes.

   The proposer is a Meta model.
2. **Run** the eval gate (ADR 0003 Amendment 1) at a fixed budget.
3. **Keep** the change only if the overall score improves, **no scenario regresses**, and the held-out scenarios don't get worse. Otherwise revert.
4. **Commit** the change with its provenance: where it came from, the scores before and after.

**Guards against gaming the metric:**
- The loop can't edit layer 5 (scenarios, labels, bars).
- A held-out scenario set is scored but never shown to the proposer.
- Entries that come from real shop or designer data stay *pending* until a person confirms them. This is the same pattern as shop photos in ADR 0005. One noisy outcome must never become a rule.

This is the "advancing" beat with evidence behind it. Anville's standard of work improves over time, and every improvement is visible in git with a before/after score. The model weights never change.

## Open questions

1. **The keystone: wrong intent tags.** If the ledger tags a face wrongly, the gate blocks a good question and nothing catches it. Should the Decide stage be allowed to *challenge* a tag (request a re-check), without being allowed to override it?
2. **Does Baseten accept `reasoning_effort` for Spark Open?** Not tested.
3. **Playbook retrieval:** situation tags first (no new service). Do we ever need embeddings?
4. **Disputed labels:** in 3 cases, all model runs disagreed with the author's label (2, 4, 9). Rayyan rules on them before the gate becomes the bar.
5. **Is the Karpathy loop core or stretch?** Its inputs (eval gate, playbook layer) are core either way. Running it overnight is cheap: about 20–40 calls per iteration, a few cents each.
6. **Demo:** show the outer loop's git history as evidence, or run it live? Live is risky; a recorded history is honest and cheap.

## Recommendation and next steps

Adopt A3 plus layers 1–5, with the Karpathy loop as the way layers 2–4 improve. If discussion confirms it, write an ADR that amends ADR 0003.

**Build order:**
1. A stage-graph skeleton on fake tools, plus the eval gate, alongside the agent loop (Oct 30).
2. Playbook v0, written by hand from Rayyan's shop knowledge. The ledger feeds episodes (already on the critical path).
3. The Karpathy loop, once the gate is stable.

**Never cut:** the second-part learning beat. It uses layer 4, which doesn't depend on the outer loop.
