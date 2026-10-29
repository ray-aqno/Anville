# ADR 0008: Geometry stack and setup-count method

- **Status:** Accepted (Rayyan Aquino, 2026-10-27)
- **Date:** 2026-10-27
- **Depends on:** ADR 0001 (STEP in, no CAD API), ADR 0002 (Decisions 3 and 5), ADR 0003 (code computes every number)
- **Feeds:** ADR 0005 (callouts map to face groups), ADR 0006 and 0007 (comments anchor to faces)

## Context

Setup count is the spine (locked decision 3). ADR 0002 fixes the contract:
- **PartFacts** come from STEP at the face level: normal, accessibility per direction (ray casting), depth, and minimum concave radius. No feature recognition.
- **Setup search** is exact brute force over subsets of the 6 principal directions.
- **Tool fit** is `diameter ≤ 2 × min_concave_radius` and `depth ≤ diameter × L:D`.

This ADR picks the library and pins down the parts the contract leaves open: how accessibility is measured, when a face counts as reachable, when 5-axis gets credit, and the `PartFacts` schema.

A throwaway prototype (OCP only, 7 synthetic parts) was run before writing this. Its numbers appear below. Labels: **verified** means checked in this session, **prior** means from memory, and **inference** means reasoned but not checked.

## Decision

### 1. Library: OCP (`cadquery-ocp-novtk`), used directly

- Pin `cadquery-ocp-novtk==8.0.1.1.0`: pybind11 bindings to OpenCASCADE 8.0.1.
  - Verified on PyPI (2026-10-27): wheels for cp311 through cp314 on manylinux_2_28 x86_64 and aarch64, macOS, and Windows.
  - The download is 67 MB and the install is about 380 MB. Its only Python dependency is a version-proxy package. The license is Apache-2.0 (verified, PyPI classifier). OCCT itself is LGPL-2.1 with an exception (prior). We link dynamically through the wheel and redistribute nothing.
- Use no build123d or CadQuery at runtime, and none in tests: the synthetic parts are built in raw OCP too (about 60 lines).
- **Modal image** (`modal_app.py` is owned by the main session):
  ```python
  .apt_install("libgl1", "libx11-6")
  .pip_install("cadquery-ocp-novtk==8.0.1.1.0")
  ```
  - Verified with `ldd`: `OCP.cpython-312-x86_64-linux-gnu.so` links `libGL.so.1`, `libGLX.so.0`, `libX11.so.6`, `libxcb.so.1`, and `libexpat.so.1`, and the wheel doesn't bundle them.
  - Inference: debian_slim lacks libGL and X11, and `libgl1` pulls in the GLX, xcb, and Xau libraries. Check with a one-line import in a `modal run` before relying on this.
- **OCCT 8 API drift** (verified): `TopoDS` is a namespace (`TopoDS.Face(s)`, not `TopoDS.Face_s`), and maps live in `OCP.collections` instead of `OCP.TopTools`. Most examples online are written for OCCT 7.x.

### 2. Conventions

- **Units and frame:** mm, in the part's own STEP coordinates. The STEP reader converts to mm. Verified: a file that declares `METRE` reads back ×1000.
- **Directions:** `+X −X +Y −Y +Z −Z`. A direction `d` points from the part toward the spindle, so the tool moves along `−d` as it enters.
- **Stock:** the blank is squared to the part's bounding box.
  - Planar faces that lie flat on the bounding box (the whole face, not only its lowest point) and face outward are **stock faces**. A face sloped by even 1° is machined. They are finished in whatever setups already exist, or left as stock, and never add a setup.
  - Every part needs at least 1 setup, so a plain block comes out as **1 setup** (`+Z`).

### 3. Accessibility: sampled ray casting

- **Samples:** UV grid cell centres on each face, kept only if `BRepClass_FaceClassifier` says they lie inside the trimmed face. Spacing is about 2 mm, with at least 8×8 and at most 32×32 per face. Slivers with no interior cells retry at 3× density. Each sample carries a point and an outward normal (flipped for reversed faces).
- **A sample is accessible from `d`** when both hold:
  - `n·d ≥ −0.01`, so walls parallel to the tool axis count, because they are cut with the side of the tool;
  - a ray from `p + 0.01·n` along `+d` hits nothing (`IntCurvesFace_ShapeIntersector` over the whole solid).
- **A face is accessible from `d` only if all of its samples are.** The fraction is stored anyway, so a partial result is visible rather than silently rounded. No test part produced a partial fraction.

### 4. Clearance facts: what limits the tool

These are raw measured facts, so the `PartEdit`s from ADR 0002 can change them and re-plan.
- **`concave_radius` and `concave_axis`:** set for concave cylindrical faces (corner fillets, holes), with the axis when it is principal.
- **`gap[d]`:** set for wall-like samples, meaning the normal projected perpendicular to `d` has length > 0.3. It is the minimum, over the face's samples, of the ray distance straight out from the wall to opposing material. It catches open slots and clevises that have no radius anywhere.
- **Adjacency:** every edge between two faces is classified by the normals at the edge midpoint:
  - `tangent` when the faces meet within 0.5°;
  - otherwise `concave` when the second face's normal leans into the first face (`n₂ · (n₁ × t) > 0`, where `t` is the edge's tangent as oriented in that face), and `convex` when it doesn't.
  - This needs no point classification, so a 5° kink is as clear as a 90° corner. A solid-classifier probe was tried first and called a 5° kink convex, because the probe landed only 0.002 mm into material (Greptile, PR #5). The two methods agree on all 248 edges at 25° or more across the test parts.

  A sharp concave straight edge parallel to a principal axis records that axis.
- **Face groups:** union-find over coplanar planar neighbours and over concave-cylinder faces with their tangent neighbours. A group is a smooth region, such as a pocket's wall ring (walls plus corner fillets) or its floor. A pocket is its wall group plus the floor group joined to it across a concave edge. Groups are the unit for edits (ADR 0002), callouts (ADR 0005), and comments.
- **Depth `depth[d]`:** distance from the stock top along `d` to the deepest point of the face, taken from the face's bounding box. This is the tool stick-out.

### 5. When a face is reachable

For face `f` in group `G` and direction `d`:

```
limit(f, d) = min over g in G ∪ concave_neighbours(G) of:
                2·concave_radius(g)   if concave_axis(g) ∥ d
                gap(g, d)             if measured
              = 0                     if G has a sharp concave edge ∥ d   (a square inside corner)
fits(tool)  = tool.diameter ≤ limit  and  depth(f, d) ≤ tool.diameter × tool.max_ld
reachable(f, d) = accessible(f, d) and some shop tool fits
```

- The largest tool that fits is reported as the face's **finishing tool**.
- Why inherit across the group: in the prototype, floors judged on their own got the wrong tool.
  - The flat bottom of a Ø8 hole was rejected, because sample chords near the rim came out at 3.9 mm.
  - Coplanar floor slivers in a 3 mm slot were given a 12 mm endmill.

  The limit belongs to the region, not to the face that happens to touch the wall.

### 6. Setup search

- **Universe:** non-stock faces that are reachable from at least one direction.
- Enumerate subsets of the 6 directions in increasing size, 64 in total, and take the first subset that covers the universe.
  - Direction order is fixed (`+Z −Z +X −X +Y −Y`), so ties break deterministically.
  - Each face is assigned to the first setup in the chosen order that reaches it.
- **Unreachable faces** are returned with a reason:
  - `no_direction`: not accessible from any of the 6 directions;
  - `no_tool`: accessible, but nothing fits. The reason reports, for each accessible direction, the diameter limit and the depth that a tool would need.

### 7. 5-axis credit: 3+2 indexed only

- A 5-axis machine reaches every direction except the one pointing into its fixture. A 5-axis plan therefore needs 1 setup if some fixture direction `f` leaves all reachable faces covered by the other five directions, and 2 setups otherwise.
- Tool fit and depth rules are the same as on 3-axis.
- Not credited: simultaneous 5-axis, compound angles, shorter tools by tilting, and the pedestal stock that 5-axis fixturing consumes. That's enough for ADR 0002's `route_machine` edit to compare 3-axis against 5-axis honestly.

### 8. Interfaces and schema

```
anville/geometry/
  facts.py    PartFacts, FaceFacts, Edge, SetupPlan dataclasses (pure, JSON-serializable)
  step.py     extract(step_path) -> PartFacts              (the only module that imports OCP)
  plan.py     min_setups(facts, shop, *, axes=3) -> SetupPlan  (pure Python, no OCP)
  shop.py     the minimal ShopProfile subset plan.py reads: tools [{name, diameter, max_ld}]
```

`PartFacts` is versioned by `schema_version: 1`. Fields are only ever added; any rename or removal bumps the version.

```
PartFacts   schema_version, source_sha256, units="mm", bbox, faces[], edges[], groups[[face_id]], extract_s,
            edge_chamfers[]   ← RFC 0002 §1: face_id, side_face, bottom_face, axis, outward, angle, leg, start, end
FaceFacts   id, surface (plane|cylinder|cone|sphere|torus|bspline|other), area, centroid, normal?,
            axis?, radius?            ← geometric signature for matching faces to Onshape face IDs
            stock, concave_radius?, concave_axis?, n_samples, stock_neighbors[],
            access{dir: {fraction, depth, gap?}}
Edge        a, b, kind (tangent|concave|convex), axis? (concave straight edges along a principal axis), radius? (set by a fillet edit; absent = sharp)
SetupPlan   axes, count, setups: [{directions: [dir], faces: [{face_id, tool}]}], unreachable: [{face_id, reason, detail}]
```

- **Face IDs** are the OCCT indices in STEP order. They are stable for the same file, but **they are not Onshape face IDs.**
- To anchor comments (ADR 0006), the connector must match faces by signature to Onshape's face list. The likely route is the Part Studio body-details endpoint. This is inference: I haven't checked it against live docs, and it costs one API call per review.
- `shop.py` covers only what the planner reads. The shop-profile format (an open question in the brief) can supersede it.

## Prototype evidence (throwaway, 2026-10-27; verified by running it)

The shop has `EM12`, `EM6`, and `EM3` endmills, all at L:D 4. Timings are local (WSL, single thread) with the 8×8 grid. At 32×32 every setup count is the same and the time is about 2.4 s at most.

| Part | Faces | Expected | Prototype | Time |
|---|---|---|---|---|
| plain block 80×50×25 | 6 | 1 (stock convention) | 1 | 0.03 s |
| top + bottom pocket (r5 corners) | 24 | 2: +Z, −Z | 2: +Z, −Z | 0.17 s |
| + Ø8 blind side hole | 26 | 3: +Z, −Z, +X | 3: +Z, −Z, +X | 0.20 s |
| 3 mm slot, 45 deep | 16 | 1; slot faces unreachable `no_tool` | 1; all 10 slot faces flagged ¹ | 0.09 s |
| pocket with r2 corners | 15 | 1; corner forces EM3 | 1; EM3 | 0.08 s |
| open 10 mm clevis, flat bottom | 10 | 1; capped at EM6 by gap | 1; EM6 | 0.06 s |
| bore inside a pocket wall | 17 | 1; bore `no_direction` | 1; bore `no_direction` | 0.09 s |

¹ Under a one-hop rule, two coplanar floor slivers got EM12. Face-group inheritance (§5) fixed that in the prototype, and no other part's result changed. It becomes a regression test.

Cost is linear in rays, at about 18 µs per ray. A part with 150 faces at 2 mm spacing should take a few seconds (inference, extrapolated).

## Options considered

**Library**
1. **OCP directly (chosen).** One wheel, every platform, smallest image, and full access to the B-rep queries we need. The cost: the API is verbose, and OCCT 8 renames break most online examples.
2. **build123d 0.13.0.** Ergonomic for building parts, but it runs on the same OCP wheel and also pulls in scipy, scikit-learn, sympy, and ipython (verified, PyPI `requires_dist`). That's more image weight for test parts only.
3. **CadQuery 2.8.0.** Pins `cadquery-ocp<8.0` together with VTK and trame (verified). Heavier, and it uses an older OCCT.
4. **pythonocc-core.** It has no wheels on PyPI (verified). It installs through conda, which fights `debian_slim + pip`.

**Accessibility**
1. **Normal-only test** (`n·d ≥ 0`). Fast, but blind to occlusion: the side bore and the in-pocket bore would both look reachable.
2. **Sampled ray casting (chosen).** It catches occlusion, and its cost is well under budget.
3. **Depth-buffer or voxel visibility.** Rendering plus GPU or extra dependencies, and a resolution set by pixels rather than geometry.

**Tool-limit propagation**
1. **The face alone.** The prototype showed it fails on floors.
2. **One hop to adjacent faces.** It fails on boolean slivers.
3. **The face group plus its concave neighbours (chosen).**

## Trade-offs

- **Finishing, not roughing.** The finishing tool is conservative: a whole pocket is reported as needing EM3 when only its r2 corners do. Setup count doesn't depend on this, but ADR 0002's cycle-time estimate should rough with the largest tool that fits the group's gap, not with the finishing tool.
- **Sampling can miss an occluder smaller than the spacing** (about 2 mm), which would undercount setups. Re-check at 4× density when a count is close to changing, or on demand.
- **Reach ignores holder clearance.** `depth ≤ D × L:D` assumes the holder clears the stock top. A deep, wide pocket next to a tall wall could still collide.
- **Principal directions only.** Compound angles and undercuts that only a T-slot cutter or a lollipop cutter can reach come out as `no_direction`. That's honest, but sometimes pessimistic.
- **All-samples-accessible is strict.** A face that two directions reach together, half each, is reported unreachable, never split. No test part does this.

## Consequences

- **Easier:**
  - `plan.py` is pure Python on JSON. Oracle tests, `PartEdit`s, and the ledger re-plans in ADR 0002 run without OCP.
  - Face groups give comments, edits, and callouts a shared unit.
  - Extraction is fast enough to run inline on every version.
- **Harder:**
  - A real Onshape STEP export has not been run through `extract`. Its face splits, tolerances, and units are unknown until the main session's spike provides one.
  - Mapping face IDs to Onshape is new connector work.
  - Developers need the OCCT 8 API names.
- **Revisit:**
  - when a demo part needs compound-angle reach;
  - when sampling at 4× changes a count on any real part;
  - when the shop-profile ADR defines tools with a reach length instead of L:D;
  - when real outcomes show the finishing-tool rule driving bad cost estimates.
- **Follow-up (Rayyan, 2026-10-27): flag precision.** Limits apply group-wide (§5), so one sharp corner marks a whole wall ring unreachable: 9 faces in `kinked_pocket`, where only 2 meet at the kink. Setup counts are unaffected, but anchored comments and fix intents would point at more geometry than is at fault. This is to be handled as a broader pass on how flags are localized to faces, not as a patch to the sharp-corner rule alone.

## Amendment 1 (Proposed, 2026-10-29): reach depends on the holder, and the facts say how

**Context.** The first real Onshape export (`tests/geometry/data/onshape_z_bracket.step`) is a Z bracket of 1/2" plate, 7" × 3" × 2.41". `extract` reads it cleanly in 1.25 s, in metres converted to mm. The plan is 4 setups on 3-axis and 1 on 5-axis (verified, `tests/geometry/test_onshape_export.py`). Its two 3/8" web holes come back `no_tool`: "needs Ø≤9.53 reaching 38.1 mm". §4 measures stick-out from the stock top, which here is the top of the −X flange. But the web is 12.7 mm thick, and the nearer hole sits 31.9 mm from the flange wall. A holder beside the flange needs 12.7 mm of stick-out, which EM6 reaches with 11 mm to spare. Any shop drills these holes, so Anville would tell the designer something false.

**Why a switch is not the fix.** Changing `depth` from "stock top" to "local material" swaps one blind spot for another:
- Stock-top depth is right when the holder is wider than the room beside the feature.
- Local depth is right only when it is narrower.
- Which one holds depends on the holder in this shop, the tool's radius, and how far the feature sits from taller material.

That last one is a design choice: move the hole away from the flange. The holder is a shop choice: a slim or shrink-fit holder. A switch hides both. The facts should carry the trade-off, and the planner, `evaluate_edits` and the model should each decide with it.

**Decision.**
1. **Facts carry a reach curve.** Add `reach` to each `access[d]`: a list of `[clearance, stickout]` steps, non-decreasing in both values.
   - It reads as: "a holder whose edge stays within `clearance` mm of the face needs `stickout` mm".
   - The first step is the local depth; the last always equals `depth`, so `depth` keeps its meaning as the worst case.
   - A face whose curve reaches `depth` within 1 mm of clearance stores `[[0, depth]]`. Every flat-topped part reads exactly as today.
   - The field is additive, so `schema_version` stays 1 (§8).
2. **How it is measured.**
   - One height map per direction: rays cast down along `d` on a 1 mm grid over the stock footprint give the top of material at each point.
   - For each face, every point higher than the face's deepest point becomes a step, at its 2D distance from the face's projected bounding box.
   - The distance is to the bounding box, not the face itself, so the clearance can only be understated, never overstated.
   - Prototype on the bracket: 0.4 s for the six height maps, 1.05 s in total for all 17 faces × 6 directions, process start included (verified).
3. **The shop states its holders.** `Tool` gains `holder_diameter: float | None`. `None` takes `ShopProfile.holder_diameter`, a stated assumption that goes on the setup sheet the way the soft-jaw sizes do (RFC 0002 §3).
   - Proposed default: 50 mm, roughly an ER32 collet nut (prior, not measured). Rayyan's shop confirms or replaces it.
4. **Fit uses the curve.** A tool of radius `r` in a holder of radius `R` sweeps the holder's edge `R − r` beyond the face. The fit rule becomes:
   ```
   stickout(f, d, tool) = first step of reach[d] whose clearance ≥ R − r + 1 mm, else depth[d]
   fits(tool)           = tool.diameter ≤ limit  and  stickout(f, d, tool) ≤ tool.diameter × tool.max_ld
   ```
   - The 1 mm is a margin for the clearance being measured on a 1 mm grid.
5. **Each side's decision is a computed number.** When the curve decides fit:
   - **Unreachable detail:** the `no_tool` reason gives both ends of the curve, e.g. "EM6 needs 12.7 mm of stick-out in a holder up to Ø67, which clears the flange 31.9 mm away, and 38.1 mm in a wider one". The model can then cite the trade-off without inventing a number (ADR 0003).
   - **Shop-side edit:** `evaluate_edits` gains an `add_tool` variant for a slimmer holder on an owned tool, with params `{name, holder_diameter}` and side `shop`. It is offered only when it changes the plan, like every other option.
   - **Design-side edit:** the clearance at which the curve steps up is the distance the feature needs from the taller wall. Proposing "move it" stays with the model, in prose, citing that number. A `move_feature` edit kind would need its own amendment to ADR 0002, and nothing in the demo needs one.

**Options considered.**
- **A. Status quo, stock-top depth.** Simple and never optimistic. Wrong on every stepped part: two false flags on the first real export.
- **B. Local depth with one fixed holder radius, a switch.** One number and cheap. But it is wrong whenever the real holder differs, can't tell the designer how far to move a feature, and has the same blind-spot shape as A. Rejected per Rayyan.
- **C. Reach curve plus shop holders. Chosen.** The facts carry the trade-off, and each consumer applies the shop's actual holder.
- **D. Full tool-assembly collision.** Sweep holder, shank and cutter solids over toolpaths with OCC booleans, the way RFC 0002 check e does for one cutter. Exact, but it needs toolpaths, and booleans for every face × direction × tool. Revisit only if C is shown wrong on a real part.

**Trade-offs and limits.**
- **Supersedes the §Trade-offs line "Reach ignores holder clearance."** That line held only because stick-out was always measured from the stock top. Under C the holder is modelled, so a wrong curve could now be optimistic. Hence the bounding-box distance, the 1 mm margin, and the worst-case `depth` kept beside the curve.
- **Holders are cylinders.** Collet-nut steps, tapers and the spindle nose are ignored. A tapered holder clears more than its nominal diameter suggests, so this is conservative (inference).
- **Material is cleared within the setup.** The height map is the final part. Everything above it is visible from `d` by construction, so the same setup can clear it before finishing the feature. That is an inference about operation order; the planner does not order operations.
- **Precision:** about the 1 mm grid. A wall thinner than the grid can be missed, the same class of limit as §Trade-offs' 2 mm sampling.

**Consequences.**
- **Easier:**
  - Stepped parts get honest reach.
  - Flags carry the numbers the designer and the shop each need.
  - The strict xfail `test_web_holes_are_reachable` becomes the regression test.
- **Unchanged:**
  - The demo. On Part A, Part B and `two_pockets`, every face reaches its stock-top depth within 0.8 mm of clearance (verified), so any real holder gives today's plans.
  - Setup counts on the bracket, since ±Z are needed anyway.
- **Harder:**
  - `extract` grows by about a second on a 7" part.
  - Each `access` entry carries a list.
  - The shop profile needs holder data.
- **Main session:**
  - The agent's facts digest should pass `reach` only where it has more than one step.
  - The `evaluate_edits` schema enum doesn't change, since this is a new `add_tool` variant, but its params gain `holder_diameter`.
- **Shop session:** `anville/shop/` (setup sheet) should print the assumed holder diameter next to the jaw sizes.
- **Revisit:**
  - when Rayyan's shop gives real holder sizes;
  - if a real part shows a curve optimistic by more than the 1 mm margin;
  - if compound-angle reach (§7) arrives, since the height map is per principal direction.
