# ADR 0010: How code ranks any-intent fixes by cost

- **Status:** Accepted (Rayyan Aquino, 2026-10-30), with the mid-range tie-break in §3 (Rayyan, 2026-10-30)
- **Date:** 2026-10-30
- **Depends on:** ADR 0009 Amendment 1 §4 (a fix valid under any intent leads; "when the cost model lands, code ranks the cheapest any-intent fix first"); RFC 0002 §4 (`cost_options`: ranged inputs, corners, verdicts); ADR 0006 §2 (ledgers)
- **Feeds:** the Decide gate in `anville/agent/review.py`; the comment's cost line

## Context

ADR 0009 Amendment 1 §4 makes a fix valid under any intent lead when intent is unknown. A fix like that is a shop-side option: buying a tool, or routing to 5-axis. The model chooses freely among those fixes until the cost model lands; then code ranks the cheapest first, and the model's freedom narrows to ties.

`cost_options` (S7, PR #20) doesn't give one price per fix. Most inputs are labelled ranges, many of them placeholders (RFC 0002 Q2, Q3), so each route costs a range at each quantity. Its verdict is either `recommended` (strictly cheapest at every corner of the input box) or "depends" with a `leader` (cheapest at the midpoint) and a `flip`. So "cheapest" needs a definition before code can rank by it.

**What the data says today** (Part A, DLL profile, every corner of 10 ranged inputs, measured 2026-10-30 on main `8a436a1`):
- **Buy the 910480 vs. route to 5-axis.** Neither is cheaper at every corner, at any scenario quantity (4, 10, 20).
  - Buying costs more at 420, 308 and 256 of the 1,024 corners.
  - At their midpoints buying is cheaper: $496 vs. $549 at q = 4, and $1,355 vs. $2,033 at q = 20.
- **What decides it is the outside rate against the in-house rate.** Buying loses where the outside shop charges $75/hr and the mill's own time is worth $120/hr.
  - Even with the outside rate never below $120/hr, buying still isn't cheaper at every corner, because the two rates vary independently in the box.
- **Comparing the ranges would mislead.** Removing the chamfer costs $65–588 at q = 4 and buying costs $230–922: the ranges overlap, yet removal is cheaper at *every* corner. Shared inputs, such as the mill rate, move both routes together. Only a corner-by-corner comparison sees that.
- **The model doesn't pick the cheaper one on its own.** Live on Part A with intent stated (orchestrator, 2026-10-30), the review led with routing to 5-axis in 10 of 10 runs and with the 910480 in 0 of 10, though buying is cheaper at mid-range inputs at all three quantities.
- **Unpriced costs bias a dollar ranking.** Removal looks cheapest only because "the designer's time to make and check the CAD edit" adds $0. Routing out has "lead time" unpriced. A route that leaves out a cost of its own can't be called cheaper on dollars alone.

## Decision (proposed)

1. **"Cheaper" means cheaper at every corner.** Fix A ranks above fix B only if, at every corner of the input box:
   - A costs strictly less than B (the difference at each corner, not overlap of the two ranges);
   - at the quantity the designer states, or at every scenario quantity when none is stated;
   - **and** every cost A leaves unpriced, B leaves unpriced too (A's `unpriced` ⊆ B's). An unpriced cost can only make a route dearer, so a route with an extra one can't be shown cheaper.
2. **This is a partial order, not a sort.** The top tier is every complete any-intent fix that no other fix outranks. Fixes in the top tier are tied, and within the tier and below it the existing order is kept (fewest unreachable, then fewest setups: `best_option`).
3. **A tie is broken by mid-range cost, when the data agrees** (Rayyan, 2026-10-30). Within the top tier, a fix leads if, with every ranged input at its midpoint, it is strictly cheaper than every other tier member at every quantity the ranking uses, and it leaves nothing unpriced that they price. If no fix is that, the tie stays open.
   - This orders the lead only. The tier, and what counts as cheaper, stay as in §1–2: a fix the midpoint favours but some corner contradicts is still tied, and the comment says so (§5).
4. **Code enforces the lead.** Decide accepts a leading proposal only from `lead` when there is one, otherwise only from the top tier. It rejects anything else with the reason ("<lead> costs less at mid-range inputs and is in the top tier"), so the retry teaches it. The fixed-script fallback proposes `lead`, or the first top-tier fix. The model still writes the proposal; with an open tie it chooses among the tier, as it does today.
5. **Ties are not upgraded.** The prose never calls a tie-break lead "recommended" or "cheapest", and its dollars stay code-rendered (the payback line, PR #26).
   - **Open:** the payback line compares the lead with the *current plan*, not with the other tier members, so it doesn't say why they are tied. On Part A it reads 3–331 parts, which shows the dependence on inputs, but a fix's range can also be narrow while the tie with another fix stays open (Codex review, 2026-10-30). A code-rendered line naming the tied fix (for example "routing to 5-axis costs less at some inputs") is the orchestrator's and Rayyan's wording call.
6. **Interface:** `anville.shop.rank_fixes(result, shop, keys, *, quantity=None) -> Ranking(top, lead, basis)`. `keys` are the plan-changing shop-side options in `best_option` order; `basis` is `dominates`, `mid_range`, `tie`, `sole` or `none`.
7. **Scope.** The ranking applies only among any-intent (shop-side) fixes that change the plan and leave nothing unreachable. Part edits stay under ADR 0009 Amendment 1 (conditional only when intent is unknown), and the current plan isn't a fix. Whether to keep the current plan stays the model's call, as now.

**Effect today:** on Part A the top tier is {buy the 910480, route to 5-axis}: neither is cheaper at every corner. At mid-range inputs buying is cheaper at 4, 10 and 20 parts ($496, $788, $1,355 against $549, $1,017, $2,033) and leaves nothing unpriced, so **the 910480 leads** (`basis: mid_range`; tested in `tests/shop/test_rank.py`). Dominance alone starts deciding when real numbers narrow the ranges: the DLL's setup and load times (RFC 0002 Q2) or an outside 5-axis quote (Q3).

## Alternatives

- **Rank by the midpoint leader alone.** Code would then order every fix by a guess: the midpoint of a placeholder range, which a quarter to two-fifths of the corners contradict on Part A. **Rejected as the ranking.** It is kept only to break a tie inside the top tier (§3), where the data has already said neither fix is cheaper everywhere, and the comment keeps saying it depends (§5).
- **Leave ties to the model.** This was the first draft. The live runs (10 of 10 routing out) showed the model doesn't pick the cheaper fix at mid-range inputs by itself, so a tie left open is decided by habit rather than by cost. **Replaced** by §3 on 2026-10-30.
- **Rank only when `cost_options` sets `recommended`.** `recommended` compares every complete route, the current plan included, so it answers a different question: whether to change the plan at all, not which fix leads. With the test inputs it recommends the current plan ($480), while ranking the fixes puts buying ($733.50) over routing out ($1,075). It also orders nothing unless one route beats *all* the others, and it ignores unpriced costs.
- **Price the unpriced costs as placeholder inputs** (a designer rate × edit time, a lead-time charge). This would let dominance run over everything, part edits included. It's left for later: it adds invented ranges, and ADR 0009 Amendment 1 already keeps part edits out of the any-intent set.

## Consequences

- **Easier:** code never ranks on a guess. When the ranking does bite, the reason is checkable: one fix is dearer at every corner.
- **Harder:** with placeholder ranges, dominance rarely bites, so most leads come from the tie-break, which rests on midpoints. The "depends" line has to carry that, and a tie-break lead can change when an input range moves.
- **Cost:** pairwise dominance costs (number of fixes)² × corners × quantities in evaluations. On Part A that's 2 × 1,024 × 3, measured at 0.02 s with `_terms` precomputed per corner.
- **Revisit** when RFC 0002 Q2 or Q3 is answered (real times or an outside quote); when a stated quantity becomes a computed input (S5); or if a third any-intent fix kind is added.

## Amendment 1 (Accepted by Rayyan Aquino, 2026-10-31): a freed face's part edits are ranked too, unless the designer says which fix

**Context.** The #37 gate's "not needed" turn proposed buying the $243 910480 in 10 of 10 runs, and the free removal in 0. The trend was 4/9, 4/10, 1/10, then 0/10. Two things kept the removal out:
- The ranking took only shop-side fixes.
- Even when given the removal, a fix that leaves a cost unpriced may never outrank one that doesn't, and a part edit always leaves "the designer's time to make and check the CAD edit" unpriced.

Rayyan, 2026-10-31: "Ranking is good, unless the designer is clearly explicit."

**Decision.**
1. **Freed faces add their part edits to the ranking.** Once a ledger constraint says a face may change (ADR 0012), its plan-changing part edits are ranked with the issue's shop-side fixes.
2. **The designer's edit time is waived for that ranking.** They volunteered the change, so their own time no longer counts against the edit. Any other unpriced cost still does.
3. **An explicit request for one kind of fix wins.** If the same reply explicitly asks for a shop fix or a part edit, only that side is ranked, and Decide and the fallback lead with it. `read_reply` records it as `prefer` with its own quote, grounded like every rule (the words must be copied from the reply), and only on a `may_change` rule.

**Consequences.**
- **On Part A after "not needed":**
  - The removal is cheaper than buying at every corner.
  - Routing out costs less at some input extremes, so the removal leads by the mid-range tie-break.
  - The comment names the route as its rival ("costs less at some inputs").
- **Harder:** a reply now carries two grounded claims, a rule and an optional preference, and each is checked separately.
- **Unchanged:** a kept or stated face is never edited (ADR 0002 Amendment 2's declined alternative excepted). An unknown face still leads with a shop-side fix.
