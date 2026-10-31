# ADR 0002: Resolve constraints on either side of the chain, by editing the world and re-planning

- **Status:** Accepted (Rayyan Aquino, 2026-10-30, by delegated review: still within the system's shape)
- **Partly superseded:** §1's tie rule, by ADR 0010 (cost-ranked any-intent fixes, mid-range tie-break).
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

## Amendment 1 (Accepted by Rayyan Aquino, 2026-10-28): `remove_feature` joins the catalog

**Context.** PR #8 (`evaluate_edits`) found that, among predicted edits, only removing a feature or routing to 5-axis lowers the setup count. Radius and depth edits change *how* a face is cut, never *from which side*. The demo parts were therefore built around a removable bottom chamfer (2 → 1 setups). Rayyan has since given that chamfer a stated intent (weld clearance). The intent protects the face, so the demo's fix became shop-side, and `remove_feature` stays as a capability for features whose intent allows it. Removal was first expressed as `set_depth` with depth 0. Three problems:
- **Depth means nothing for a chamfer.** "Set the depth of this chamfer to 0" reads as nonsense to the designer and to the model.
- **Removal doesn't depend on direction, but depth does.** The demo's bottom-edge chamfer is fully visible from −Z, from −Y, and along its own length from ±X (measured with `extract`). `set_depth` would have offered the same removal once for each of those directions.
- **The CAD action is "suppress", not "resize".** Spike 0001 shows the connector edits by "read feature → patch → update → verify". Suppression is one more patch: the feature's `suppressed` flag. **Verified 2026-10-28 by the main session:** `suppressed: true` on a feature update returned `featureStatus: OK`, the face count went from 13 to 12, and it took 2 Onshape calls.

**Decision.**
1. Add `PartEdit: remove_feature`. It drops the named group plus the groups that share a concave edge with it (the same "feature" scope as the other part edits). The connector compiles it to suppressing the CAD feature that made those faces.
2. `set_depth` no longer proposes depth 0. It only proposes depths that a fitting tool reaches.

**Consequences.**
- **Removal must respect stated intent.** A face whose intent is stated (RFC 0001's gate) is protected, so `remove_feature` on it is a violation for the tool layer to reject before scoring (ADR 0003 Amendment 1), not a fix.
- **Predicted vs. measured:** on both demo parts, the predicted removal matches re-extracting the part with the chamfer suppressed: 1 setup and 0 unreachable faces in both (`tests/geometry/test_demo_parts.py`).
- **Where prediction stops being exact:** a removed feature that *defines* the bounding box (for example a boss that sets the part's height) changes which faces count as stock, and dropping faces can't predict that. Such removals need the measured path.
- **Main session:**
  - the agent's `evaluate_edits` schema enum gains `remove_feature`, so ADR 0003's eval gate re-runs on that schema change;
  - the connector needs a map from a face group to the Onshape feature that made it (one `features` read; the spike found deterministic IDs).
- **Revisit:** if a demo needs partial removal (one edge of a multi-edge chamfer feature), since suppression is all-or-nothing per CAD feature.

## Amendment 2 (Proposed, 2026-10-30): a protected face's removal may be shown as the declined alternative

**Context.** Rayyan chose demo beat (c) on 2026-10-30. Geometry's RFC 0005 found that on Parts A and B the only part edit that lowers setups is removing the bottom chamfer, and that chamfer's intent is stated (weld clearance). Amendment 1 says such an edit is "rejected before scoring", so it can never be built or shown. Beat (c) needs it to be shown, honestly, as the alternative the designer declines: "or remove the chamfer: 1 setup, but it loses weld clearance".

**Decision.**
1. **Never recommended, never merged.** An edit on a protected group still can't lead, can't be proposed, and is never applied to the designer's workspace. Decide's protected-face filter (ADR 0009 §4) is unchanged.
2. **It may be scored and built as the declined alternative.** Code (not the model) may score it and build it on an `anville/alt-<run>` branch through the connector, labelled *measured* once the branch's STEP is re-counted.
3. **It's always stated with its intent cost,** in a code-rendered line next to the recommended shop-side fix: "Or remove G3 on a branch: N setup(s), but it loses <stated reason>."
4. **Only after the recommended fix,** never instead of it (flag, then suggest).

**Consequences.**
- **Easier:** the fix-branch beat runs on the demo's own Part A, so RFC 0005's proof calls can target it, not a proof copy.
- **Harder:** the scorer runs some edits it then refuses to recommend. The trace must label them "declined alternative", so the panel never shows them as options.
- **To revisit:** whether a designer's "not needed" reply can lift the protection (today only a ledger `may_change` rule does, per ADR 0012).
