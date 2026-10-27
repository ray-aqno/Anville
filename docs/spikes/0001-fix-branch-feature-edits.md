# Spike 0001: Fix branch — can Anville edit a part through the Onshape API?

- **Date:** 2026-10-27
- **Answers:** the brief's open question "Fix branch feasibility" (ADR 0001 connector, ADR 0002 decision 4, ADR 0007 live edits)
- **Result:** **Yes.** Workspace creation, adding a feature, and editing an existing feature's dimension all work, rebuild cleanly, and appear live in an open Onshape tab.
- **Cost:** 7 Onshape calls in total.

## What was run

The test part is a synthetic Part Studio (Sketch 1 → Revolve 1 → Sketch 2 → Extrude 1, a through-all cut of six 0.25 in holes). Every call went through `OnshapeClient` (the budgeted client).

| Step | Endpoint | Calls | Outcome (verified) |
|---|---|---|---|
| 1. Branch | `POST /documents/d/{did}/workspaces` with `versionId` | 1 | Created workspace `anville/fix-spike-1` from a saved version. The designer's main workspace is untouched. |
| 1b. Read | `GET .../features`, `GET .../bodydetails` | 2 | Feature list and B-rep: 12 faces, 18 edges, deterministic edge IDs (for example `JHB`, the outer 44.45 mm circular edge). |
| 2. Add a feature (`PartEdit` that adds geometry) | `POST /partstudios/d/{did}/w/{wid}/e/{eid}/features` | 1 + 1 verify | A fillet, `entities = [JHB]`, `radius = 2 mm`. `featureStatus: OK`. Faces went from 12 to 13 and edges from 18 to 19, as expected for one rounded circular edge. |
| 3. Edit an existing dimension (`PartEdit` that changes intent) | `POST .../features/featureid/{fid}` | 1 + 1 verify | The six `DIAMETER` constraints in Sketch 2 went from 0.25 in to 0.266 in. `featureStatus: OK`. Six cylinder faces changed from 3.175 mm to 3.378 mm radius. The downstream fillet still rebuilt (13 faces). |

**Seen in the browser (reported by Rayyan):** the added fillet appeared in an already open tab without a refresh. It looked right.

## What we learned

1. **The request shapes match the OpenAPI spec** (`BTFeatureDefinitionCall-1406` → `BTMFeature-134` / `BTMSketch-151`; `BTMIndividualQuery-138.deterministicIds` for edges). For the fillet, the parameter IDs `entities` and `radius` were guessed from memory, and the API accepted them.
2. **A sketch-dimension edit means sending the whole sketch back.** The update call takes the full feature definition with only the `length` expression changed. The connector should therefore read the feature, patch it, and write it back. It should never build a sketch from scratch.
3. **Verification is cheap.** One `bodydetails` read (face count, surface types, radii) confirms the edit did what was intended before any STEP export. The full proof is still the planner re-counting setups from re-exported STEP (ADR 0002 decision 4, "measured"), which is the next step.
4. **Live propagation works with plain REST calls.** That supports ADR 0007's "watch it edit" without screen control.
5. **Attribution:** the feature *name* can carry "Anville · …", but the edit history still shows the API key owner. ADR 0007's separate Anville identity is still needed.

## Not yet tested

- Exporting STEP from the branch workspace, then re-running setup count on the edited part (this depends on the geometry layer).
- Concurrent edits: the designer changes main while Anville edits the branch. `sourceMicroversion` and `rejectMicroversionSkew` exist in the spec but weren't exercised.
- Edits whose deterministic IDs come from a different microversion than the one they were read on (`idtranslations`).
- Merging the branch back into main. That's left to the designer, on purpose.

## Consequence

The fallback (a written fix plus marked-up render) stays as the fixed-script path, but the primary path is now real CAD edits on a branch. The connector's job: compile a CAD-neutral fix intent (ADR 0001) into "read feature → patch parameter → update feature → verify via `bodydetails`".
