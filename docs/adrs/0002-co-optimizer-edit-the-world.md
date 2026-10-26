# ADR 0002: Resolve constraints on either side of the chain, by editing the world and re-planning

- **Status:** Proposed
- **Date:** 2026-10-26

## Context

When a part hits a manufacturing constraint (a radius too small for available tools, a face needing an extra setup), the usual answer is to change the design. Often a shop-side change is cheaper: a long-reach endmill, soft jaws, or routing to a 5-axis machine. Nobody compares these options because the costs live on different sides of the design-to-shop handoff.

Constraints on the design: setup count is computed from geometry, never guessed (locked decision 3). Meta models do the reasoning; everything else is deterministic code.

## Decision

1. **The objective is total dollars at the actual quantity.**
   ```
   batches(q)      = ceil(q / min(batch, q))
   part_cost(w, q) = setups(w) × setup_cost × batches(q) + q × Σ(op_time × rate(machine))
   ```
   - Shop-side changes are also scored across every part in the ledger. A change that saves nothing (`ledger_savings ≤ 0`) is never recommended.
   - Ties go to fewer setups, then to a shop-side edit when design intent is unknown, then to lower one-time cost.
2. **Every resolution is an edit to the world** (`world = PartFacts + ShopProfile`):
   - `PartEdit`: `set_min_radius`, `set_depth`
   - `ShopEdit`: `add_tool`, `add_fixture`, `route_machine`
3. **One deterministic planner** evaluates every candidate. It runs on a copy of the edited world and diffs setups and dollars against the original.
   - Setup search is exact brute force over subsets of the 6 principal directions (64 subsets).
   - Tool fit is `diameter ≤ 2 × min_concave_radius` and `depth ≤ diameter × L:D`.
4. **Execute, then verify.**
   - A `PartEdit` becomes a CAD edit through the connector (ADR 0001). It is **measured** when the new STEP is re-extracted, and **predicted** when it comes from the written-fix fallback.
   - A `ShopEdit` produces a ledger entry, an artifact (purchase spec, fixture note, or routing change), and an updated setup sheet. It is **predicted** until a real shop outcome is logged.
   - Labels say which is which.
5. **PartFacts come from STEP at the face level.** For each face: normal, accessibility per direction (ray casting with OCC), depth, and minimum concave radius. Full feature recognition is out of scope.

## Options considered

1. **Recommend all four branches, execute only design edits.** Smallest build, but the shop-side branches stay advisory only.
2. **A dedicated executor and verifier per branch.** Four separate code paths; too much hidden complexity.
3. **Edit the world, with one planner (chosen).** Every branch is executed and verified through one path.

## Trade-offs

- Shop-side "execution" stops before the physical act: a person still buys the tool. That has to be stated precisely, not overclaimed.
- Cycle time is an estimate (removal volume ÷ rate per tool class). Setup count is exact. At low quantities setup cost dominates, which limits the effect of the estimate error.
- Six principal directions don't cover undercuts or compound angles.

## Consequences

- **Easier:** adding a new resolution type means adding one catalog entry and one planner effect. Payback across many parts comes for free from re-planning the ledger.
- **Harder:** the planner is load-bearing. A bug there skews every recommendation, so it needs oracle tests on known parts.
- **Revisit:** when demo parts need reach beyond the 6 axes, or when real outcomes show cycle-time error dominating decisions.
