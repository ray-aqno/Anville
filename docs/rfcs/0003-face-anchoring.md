# RFC 0003: Anchoring flags to Onshape faces

- **Status:** Closed: the anchoring itself is deferred past the hackathon (Rayyan, 2026-10-31). The face matcher this RFC proposed is built (`pair` in `anville/geometry/anchor.py`), but only its probe (`scripts/probe_face_anchoring.py`) calls it; the fix branch matches faces by area and centroid instead (`match_centroids`, RFC 0005). Pinning a comment to its faces and the panel's "Show in model" are not built; the flagged face is shown instead by a render painted by code from the B-rep, with SAM 3.1's outline when its mask agrees (ADR 0007 Amendment 4, PR #50).
- **Date:** 2026-10-30
- **Author:** geometry session (for S4: comment anchoring and the panel's "Show in model")
- **Touches:** ADR 0006 §10 (comment anchoring), ADR 0007 (panel selection and highlight), ADR 0008 §8 (face IDs are OCCT indices)

Labels: **verified** means checked, with the source given. **Prior** means from memory. **Inference** means reasoned but not checked.

## Problem

Every number Anville states is computed on face groups from `extract` (ADR 0008). A group's faces are OCCT indices, numbered by `TopExp.MapShapes` traversal of the solid (not necessarily the file's entity order), and Onshape knows nothing about them. Two features need Onshape's own face IDs:
1. **Comment anchoring (ADR 0006 §10).** A flag should sit on the faces it is about, not on the document.
2. **"Show in model" (ADR 0007).** The panel should light up a flag's faces with `requestSelectionHighlight`.

So the question is: given a version's STEP file and its PartFacts, which Onshape face ID is each of our faces?

## What is known

- **The STEP file carries no Onshape IDs.** All 17 `ADVANCED_FACE` entities in the first real export are named `''`, and there are no attribute entities (verified, `tests/geometry/data/onshape_z_bracket.step`). Whether some export option adds names is unknown.
- **Comment anchoring works.** `elementQuery: 'query=qTransient("<faceId>");'` on a saved version anchors to a single face (verified 2026-10-27, ADR 0006). IDs are valid for one microversion; `POST .../idtranslations` maps them between versions (verified, ADR 0006).
- **Highlighting takes the same IDs, several at once.** The right-panel messaging docs give `requestSelectionHighlight` a `selections` list of `{selectionType: "ENTITY", selectionId: "JHO", entityType: "FACE"}`, and say "Call getPartStudioBodyDetails to get the `selectionId`" (verified, [Element Right Panel docs](https://onshape-public.github.io/docs/app-dev/messages/element-right-panel/)). The docs don't say whether these are transient or deterministic IDs, or which microversion they belong to.
- **`bodydetails` describes every face:** `id`, `area`, `box`, `loops`, `orientation`, `surface` (with `type`, `origin`, `normal`). Source: the 2021 Java client schema (verified for the schema, [PartStudiosGetBodyDetailsResponseBodiesFaces](https://onshape-public.github.io/java-client/com/onshape/api/responses/PartStudiosGetBodyDetailsResponseBodiesFaces.html)). Spike 0001 saw it return radii on a live part (verified). The `box` field names and the units (metres, prior) are not checked.
- **The job exports STEP from a version path** (`/partstudios/d/{did}/v/{vid}/e/{eid}/translations`, verified in `anville/modal_app.py`). A `bodydetails` read on the same path describes the same geometry, at one extra call per review (inference: same path, same B-rep).

## Constraints

- The core stays CAD-agnostic: geometry comes from STEP (locked decision; ADR 0001). Onshape IDs are a connector concern, attached after `extract`.
- The Onshape quota is shared and the main session's. At most one extra call per review.
- **Never guess an anchor.** A flag on the wrong face is worse than a flag on the document. Ambiguity must fall back visibly.

## Options

**A. Match by geometry at the same version (recommended).** Read `bodydetails` at the version whose STEP was analyzed. Pair each of our faces with the Onshape face of the same surface type, the same area (±0.5%), and the same bounding box (worst corner within 0.05 mm). Accept a pair only when the best box error beats the runner-up by 10×. Otherwise the face is ambiguous and stays unanchored.
- *For:* one GET per review; pure Python after the read; keeps the core STEP-only.
- *Against:* depends on Onshape's boxes being as tight as OCCT's (unknown for curved faces), and on both kernels splitting faces the same way (unknown: Parasolid vs OCCT seams).
- *Offline evidence (verified, `scripts/probe_face_anchoring.py --from-json`, against a `bodydetails` response synthesized from our own bracket in metres, shuffled, uppercase types):*
  - with boxes, all 17 faces pair correctly and the scale is detected as 1000;
  - with type and area alone, 7 of 17 are unique, but uniqueness says nothing about *where* a face is (a lone 100 mm² plane could be the mirror image). The probe therefore pairs none of them without a box; the symmetric pairs (the two web holes, the two flange tops) would tie anyway.
  
  **Position is the keystone.**

**B. FeatureScript evaluation.** `POST .../featurescript` returns, for every face, its transient ID with `evArea` and `evBox3d`. Exact on Onshape's side, in one call.
- *For:* no tolerance on Onshape's description; FeatureScript reports exactly what we ask for.
- *Against:* a FeatureScript program to maintain; its result format is more work to parse; still needs the same matching against our faces. It's the fallback if A's boxes prove loose (prior: the endpoint exists; its exact output wasn't checked).

**C. Compute PartFacts from Onshape directly** (tessellation or `bodydetails`), skipping STEP. Rejected: it breaks the CAD-agnostic core and loses exact OCCT geometry.

**D. Anchor to the document and point with a render** (RFC 0004). The current fallback, and what an ambiguous face gets under A.

## Proposed design (if A holds)

1. **PartFacts:** `FaceFacts` gains `box: [[x,y,z],[x,y,z]]` in mm. This is additive, so `schema_version` stays 1. `extract` already computes it.
2. **Geometry, pure:** `match_faces(facts, bodydetails) -> {face_id: onshape_id | None}`.
   - It detects the unit scale as the one under which most of our faces find an equal-area Onshape face. Total area would mislead, because the Part Studio may hold other bodies. It never assumes metres.
   - It returns `None` for unmatched, ambiguous or box-less faces, and when two of our faces claim one Onshape ID. Never a best guess.
   - Unknown Onshape surface names are reported and never matched.
   - It lives in `anville/geometry/anchor.py`, with no OCP.
3. **Connector (main):**
   - one `bodydetails` GET on the analyzed version;
   - the map is stored with the run;
   - a comment anchors with `qTransient` to **the face its finding was measured on**: the fillet for a radius flag, the chamfer for a setup flag. Not the group's largest face, which could be a neighbouring wall. Single-face anchoring is verified; a multi-face query is not.
   - If that face didn't match, the comment goes on the document (5), even when other faces of the group matched.
4. **Panel (main):** "Show in model" sends **every matched face of the group** in one `requestSelectionHighlight` (a list is verified).
   - The panel views the workspace, not the version, so the IDs go through `idtranslations` first (ADR 0007's existing path) whenever the workspace microversion differs.
   - A face that doesn't translate isn't highlighted, and the panel says so.
5. **Fallback, always visible:** a flag whose faces don't match is posted on the document. It says which face it means, in words and by its group label, and the RFC 0004 render points at it (`marker`: a ring on a hole, an arrow on a chamfer).

## Probe (main runs it; 1 call)

`scripts/probe_face_anchoring.py --did D --vid V --eid E --step <V's STEP> --out bodydetails.json`

It makes one GET to `bodydetails` on the version, saves the raw response, matches it, and prints the result. It is read-only. Run it on the version of the Z bracket whose STEP is `tests/geometry/data/onshape_z_bracket.step`, and on Part A once it's modeled from the build sheet. It settles:
1. the `box` field names and units, from the printed sample;
2. whether Onshape and OCCT have the same face count (Parasolid seams vs OCCT);
3. the match rate, and the box residuals on planes vs cylinders;
4. any surface names outside the 2021 schema, listed under `unknown_surface_types`;
5. through the saved JSON, the rest of `bodydetails` (radius fields, loops), with no further calls.

It checks its arguments and reads the STEP before the call, so a typo can't waste it.

Not covered by the probe:
- whether `qTransient` accepts several faces;
- whether IDs survive into the workspace unchanged (`idtranslations`).

Each needs one more call, and neither blocks A.

## Open questions

- **Box tightness:** if Onshape's boxes are loose on curved faces, widen the tolerance for cylinders only, or move to B.
- **Face splitting:** if the kernels split differently (a full cylinder as one face or two), match many-to-one by summed area within the same box.
- **Frames:** does the STEP export use the Part Studio's world frame, as `bodydetails` does? (Inference: yes. The probe's box residuals answer it.)
- **Assemblies:** out of scope. Anville reviews Part Studios.

## Recommendation

Option A, with no anchor rather than a guessed one. Run the probe on the bracket's version first. If boxes match to 0.05 mm with equal face counts, write the ADR and build `match_faces` (about two hours, all in geometry). If they don't, the saved JSON tells us whether to loosen tolerances or switch to B, without spending another call.
