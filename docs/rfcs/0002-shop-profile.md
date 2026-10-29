# RFC 0002: Shop profile, narrowed to one provable shop-side fix

- **Status:** Draft (revised after two Codex review passes, 2026-10-28)
- **Date:** 2026-10-28
- **Answers (in part):** the brief's open question "Shop profile format: machines, tool library, stock, fixturing, setup-cost rule"
- **Touches:**
  - ADR 0002 (the objective is $ at quantity; `ShopEdit`s);
  - ADR 0002 Amendment 1 (`remove_feature` is rejected on stated-intent faces);
  - ADR 0008 (the planner's `ShopProfile`).

## Problem

The planner knows a shop only as a list of flat endmills. The demo now depends on one shop-side fix the planner can't represent. Part A's bottom weld-clearance chamfer has stated intent, so it stays; the only thing forcing a second setup is that chamfer. The proposed fix is to **finish it from +Z with a back-chamfer cutter, held in relieved soft jaws**, and compare that, in dollars, with the alternatives a machinist would weigh.

**Scope decision (Codex review, finding 7):** prove this one capability end to end on Parts A and B first. A general typed-tool schema (drills, chamfer mills, many fixtures) comes after the demo works and gets its own RFC.

## The demo shop (sources tagged)

| Field | Value | Source | Confidence |
|---|---|---|---|
| CNC mill | TRAK TMC5 toolroom VMC, 3-axis, ProtoTRAK RMX | Marquette DLL public equipment list; current TRAK generation confirmed by Rayyan | verified |
| Table, travel, spindle | 27.56 × 15.75 in table; 20 × 16 × 20 in travel; 40-taper; 8,000 rpm | dealer spec listings | secondary |
| Manual mills | 2× TRAK K3 knee mills | DLL list | verified |
| 5-axis | none in-house | DLL list | verified, so 5-axis means outsourcing (out of scope here) |
| Workholding | 6″ vise, aluminum soft jaws, parallels | Rayyan: "the full package on the mills" | **stated, not confirmed with the lab** |
| Back-chamfer cutter | Harvey Tool **910480**: 45°, head 0.500″ (12.70 mm), neck 0.317″ (8.05 mm), neck length 2.5″ (63.5 mm), length of cut 0.088″ (2.24 mm), **$243.50** | harveytool.com, checked 2026-10-28 | verified |
| Rates | 3-axis job shop about **$40–120/hr** (most quotes $60–100) | 2026 vendor pricing guides (fabcon, rapiddirect, jiga) | sourced range; marketing-driven |

## Proposal

### 1. A recognizer for exterior edge chamfers (Codex finding 3)

**The rule.** A face is an *exterior edge chamfer* when all of these hold:
- it's **planar**;
- its normal is **45° ± 0.5°** from Z, pointing down and outward;
- it **bridges a stock side face and the stock bottom face** (it shares an edge with each).

Its **leg** is measured **perpendicular to the edge**: the face's width in the cross-section through the side and bottom faces, divided by √2. It is never the face's length or diagonal (Codex pass 2). The rule is general and deterministic: it excludes drafted walls, which don't bridge two stock faces, and interior chamfers, which don't touch a stock side face. It covers any straight exterior edge chamfer. Curved edges and chamfers on other angles are out of scope for now. It needs one addition to `FaceFacts`: which stock faces a face touches. That's additive, so `schema_version` stays 1.

### 2. A back-chamfer access rule that checks the cut, not just the approach (Codex finding 1)

A back-chamfer tool `T` finishes an exterior edge chamfer `F` from +Z only when every check passes:

| # | Check | Part A/B (1.5 mm leg, 20 mm tall) with 910480 |
|---|---|---|
| a | The cutter angle matches the chamfer angle (45°) | 45° = 45° ✓ |
| b | Leg ≤ length of cut | 1.5 ≤ 2.24 ✓ |
| c | **Neck clearance:** (head − neck)/2 − leg ≥ 0.5 mm | (12.70 − 8.05)/2 − 1.5 = **0.82 mm** ✓ (a 2 mm leg would leave 0.32 mm: ✗) |
| d | Neck length ≥ stock height + 5 mm | 63.5 ≥ 25 ✓ |
| e | **Swept solid, not rays (Codex pass 2):** for a straight edge, sweeping the head and the neck along the cutting pass, overtravel included, each traces a prism with rounded ends. Build both as OCC solids. The intersection of each with the part, apart from the chamfer material being removed, must be empty. | OCC boolean in `extract` |
| f | **Workholding:** the same swept solids must not intersect simple box models of the jaws (with their reliefs) or of the parallels (§3) | OCC boolean |

If any check fails, the face stays in its −Z setup, and the plan says which check failed and by how much (the geometry session's negative tests). The checks prove **nominal** clearance from catalog dimensions. The setup sheet still asks the machinist to verify the toolpath at the machine.

### 3. Workholding: one named fixture, explicit and checkable (Codex finding 2)

- **The fixture:** a 6″ vise with **aluminum soft jaws gripping the faces perpendicular to the chamfered edge** (Part A: the X ends; Part B: the ±Y faces). The part sits on parallels set back at least head radius + 1 mm from the chamfered side.
- **Jaw relief:** each jaw gets a pocket at the bottom corner on the chamfer side. Its size is **derived from the swept head solid** (check f) plus 1 mm, not chosen by hand, so the head can overtravel past both ends of the edge.
- **The setup sheet states all of it:** the jaws, the reliefs, and the parallel positions. If the shop holds the part differently, the 1-setup plan doesn't apply, and the sheet says so.
- **Cost:** soft jaws are a one-time cost (blank plus about 30 min of machining). They're **reusable for Part B** when the stock sizes match, and are re-cut otherwise.
- **Confidence:** stated by Rayyan. A DLL machinist should confirm it before the demo recording.

### 4. Cost: one operation model for every route, decided by interval bounds (Codex findings 4–6)

**Every route is costed with the same components:**

```
cost(route, q) = one_time
               + Σ over operations o in the route:
                   batches(q) × setup_time(o) × rate(machine(o))
                 + q × (load_time(o) + cut_time(o)) × rate(machine(o))
```

Each operation is priced at **its own machine's rate** (Codex pass 2), so R2's TMC5 operation and its K3 operation use different rates.

**Three routes for Part A's chamfer:**

| Route | Setups / batch | One-time | Per-part extra |
|---|---|---|---|
| **R1. CNC flip** (today's plan) | 2 on the TMC5 | none | a second load, plus chamfer cut time |
| **R2. Manual chamfer on a K3** | 1 TMC5 + 1 K3 | none | a K3 load plus manual chamfer time, at the K3 rate |
| **R3. Back-chamfer, relieved soft jaws** | 1 | cutter $243.50 + soft jaws | back-chamfer cut time |

**One-time costs are spread over every part they serve.** The cutter bought for Part A also serves Part B, which is how the learning beat shows up in the dollars.

**The decision uses exact bounds over every combination of inputs, not low/high samples (Codex, both passes).**
- **Why corners:** a cost difference multiplies uncertain inputs together (time × rate), so it isn't linear. It is *multiaffine*, though: linear in each input when the others are held fixed. A multiaffine function over a box of ranges takes its minimum and maximum at the box's **corners**. With about 8 uncertain inputs that's 2⁸ = 256 corners, so evaluating all of them is cheap and exact.
- **Quantities aren't uncertain inputs.** They're fixed scenarios (for example q = 4, 10, 20), each evaluated separately, so the step in `batches(q)` never falls inside the box.
- **R3 must beat both alternatives:** report R3 as **recommended** only if R3 − R1 < 0 **and** R3 − R2 < 0 at every corner. The same rule applies to any route being recommended.
- **Otherwise it's "depends"**, shown with the corner where the recommendation loses: the actual combination of rate, times and price that flips it, instead of a single "driver" input.
- **Payback** is reported as a range of parts, found over the same corners.

**Tool choice optimizes over tool *and* direction together, by cost (Codex finding 6).** No "ordinary tool first" rule. The special cutter is eligible from +Z even where an ordinary tool could cut the face from −Z, and cost decides between them. This also stops a costly special tool from being assigned to the top edge break, where it saves nothing.

## Options considered

| Option | For | Against |
|---|---|---|
| **A. Narrow: one recognizer, one cutter, one fixture, three costed routes (proposed)** | Provable on Parts A and B in 7 days; every claim can be checked | Not a general shop model yet |
| B. Broad: the typed-tool schema for every tool type (the first draft) | General | Codex: too broad for 7 days, and `FaceFacts` can't yet back most of its rules |
| C. Full fixture modelling (vise geometry, clamp collision) | Exact | Days of geometry work, outside the wedge |

## Open questions

1. **Workholding confirmation:** would a DLL machinist hold Part A in relieved soft jaws as described?
2. **Time inputs:** setup, load, manual-chamfer and back-chamfer cut times. Ranges with sources, or labeled assumptions. These dominate R2 vs. R3.
3. **Quantities:** what batch sizes and counts make the demo honest for a Baja-style team (for example 4–20 parts a season)?
4. **The K3 rate:** a manual operation runs at a lower rate but takes longer. Use one rate with a wider range, or a separate labor rate?

## Recommendation

Adopt Option A.
- **Geometry session:** the recognizer (§1) and the access rule (§2), with the tests it already specified, plus the neck-clearance check and the swept-solid checks against the part and the jaw and parallel boxes.
- **Main session:** the per-operation cost model with corner enumeration (§4), and the setup-sheet lines for the fixture, the tool type and the machinist's toolpath check (§3).
- **Then an ADR,** after the demo path works on Parts A and B.
