# RFC 0005: Fix branch feasibility, from edit catalog to Onshape feature edits

- **Status:** Draft
- **Date:** 2026-10-30
- **Author:** geometry session (requested by the orchestrator session)
- **Touches:** brief §4 "Fix branch feasibility", brief §7 (demo beat "Anville opens a fix branch → setups drop"), Spike 0001, ADR 0001 (connector), ADR 0002 and its Amendment 1, locked decision 5 (Muse Code writes the fix), RFC 0003 (face anchoring), RFC 0004 (render)

Labels:
- **verified-live** means seen working against Onshape, with the source given.
- **verified-spec** means read in the OpenAPI spec fetched today (`https://cad.onshape.com/api/openapi`, version `1.223.92697`, unauthenticated, no quota call).
- **verified-guide** means read in the linked Onshape API guide page; FeatureScript functions are not in the spec.
- **prior** means from memory. **Inference** means reasoned but not checked.

## Summary

The brief's question, "can Anville edit a part through the API?", **is already answered yes, live**:
- **Spike 0001 (2026-10-27, 7 calls):** it branched a workspace from a version, added a fillet and edited a sketch's diameter constraints. Everything rebuilt cleanly and appeared in an open tab.
- **ADR 0002 Amendment 1 (2026-10-28, main session, 2 calls):** `suppressed: true` on a feature update returned `featureStatus: OK`, and the face count went from 13 to 12.

Two things are **not** proven:
1. **The mapping.** Our edits name a **face group** (`G3`); Onshape edits name a **feature** (`featureId`). ADR 0002 Amendment 1 lists this map as the connector's job, and nothing builds it yet.
2. **The re-count.** No STEP has been exported from a branch workspace and re-counted.

This RFC closes the mapping on paper and names the 7–8 calls that would prove one edit end to end. It also flags a conflict between the brief's demo beat and Part A's protected chamfer (see "The demo beat").

## Endpoints

| Step | Endpoint | Label |
|---|---|---|
| Branch | `POST /documents/d/{did}/workspaces` with `versionId` | verified-live (Spike 0001), verified-spec (`createWorkspace`) |
| Read the feature list | `GET /partstudios/d/{did}/{wvm}/{wvmid}/e/{eid}/features` (query: `featureId`, `includeGeometryIds`, `rollbackBarIndex`) | verified-live (Spike 0001), verified-spec |
| Parameter IDs per feature type | `GET .../featurespecs` | verified-spec |
| **Update or suppress one feature** | `POST /partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{fid}`; the response (`BTFeatureDefinitionResponse-1617`) has `featureState` | verified-live: a sketch sent back whole (Spike 0001); `suppressed: true` (ADR 0002 Amendment 1). verified-spec for the response field |
| Update several features at once | `POST .../w/{wid}/e/{eid}/features/updates` (`BTUpdateFeaturesCall-1748`): "updates only the parameters supplied in the top-level feature structure, and optionally can update feature suppression attributes". The response (`BTUpdateFeaturesResponse-1333`) has `featureStates` | verified-spec. **Not run live**, and not needed: the single-feature path above is proven |
| Delete | `DELETE .../features/featureid/{fid}` | verified-spec. Not used: suppression is reversible, deletion isn't |
| FeatureScript | `POST .../{wvm}/{wvmid}/e/{eid}/featurescript` `{script, queries}`; the response (`BTFeatureScriptEvalResponse-1859`) has `result`, `console` and `notices` | verified-spec for the endpoint and shapes. "Only lambda expressions can be evaluated" is verified-guide ([FS guide](https://onshape-public.github.io/docs/api-adv/fs/)) |
| STEP from the branch | `POST /partstudios/d/{did}/{wv}/{wvid}/e/{eid}/translations` with `wv` "One of w or v", then `GET /translations/{tid}` and `GET /documents/d/{did}/externaldata/{id}` from `resultExternalDataIds` | verified-spec for `w`. The body needs **`storeInDocument: false`** to get external data: the spec says so, and gives the field no default. The version path with that body is verified-live (`anville/modal_app.py:314-327`) |
| Concurrency guard | `sourceMicroversion` plus `rejectMicroversionSkew: true` on the edit | verified-spec. Not run (Spike 0001 lists it as untested) |

## The missing link: face group to feature

**Recommended: read the feature list, then one FeatureScript call that returns, for each feature, the faces it created with their surface type, area and box.**

```
function(context is Context, queries) {
    var out = {};
    for (var fid in queries.features) {          // feature IDs from GET .../features
        var faces = [];
        for (var f in evaluateQuery(context, qCreatedBy(makeId(fid), EntityType.FACE))) {
            faces = append(faces, { "id" : transientQueriesToStrings(f),
                                    "surface" : evSurfaceDefinition(context, { "face" : f }),
                                    "area" : evArea(context, { "entities" : f }),
                                    "box" : evBox3d(context, { "topology" : f, "tight" : true }) });
        }
        out[fid] = faces;
    }
    return out;
}
```

- **Labels:**
  - verified-guide: `qCreatedBy(makeId(...), EntityType.FACE)`, `evaluateQuery`, `transientQueriesToStrings` and `evBox3d` (the guide's two examples).
  - prior: `evArea` and `evSurfaceDefinition`, and passing the feature IDs in through `queries`.
  - Whether `makeId` accepts a REST `featureId` string as-is: inference.
- **"Created by" is the right relation for this job.** Suppressing a feature removes the faces it *created*. A face a later feature only *modified* (trimmed, split) still belongs to its creator for `remove_feature` (inference).
  - An alternative is `lastModifyingOperationId` (prior; not in the guide). It answers a different question, which feature last touched a face, and is the wrong target for suppression.
- **One call serves two RFCs:**
  1. **RFC 0003's anchoring:** the same transient IDs with surface type, area and box. That is RFC 0003's Option B, exact on Onshape's side, and it could replace the `bodydetails` read.
  2. **This RFC's mapping:** each of our faces is matched to an Onshape face with `pair()` from RFC 0003's probe, on surface type, area and box. Each of our groups then maps to the feature(s) that created its faces.
- **A group made by several features** maps to all of them, and the edit must name one. For example, a pocket's walls come from an extrude while its corner fillets come from a fillet feature.
- **A face that doesn't match gets no feature,** and the edit falls back (below). It is never guessed (the RFC 0003 rule).

**Not recommended beyond a proof: matching by feature name.** The demo parts name their features ("Weld clearance chamfer", build sheet). A designer's part says "Chamfer 3".

## Edit catalog to Onshape edit

| Our edit (ADR 0002) | Onshape edit | Kind | Label |
|---|---|---|---|
| `remove_feature` on G | **Suppress** the feature that created G: `suppressed: true` on the single-feature update | Feature-level; reversible; one call | verified-live (ADR 0002 Amendment 1). **Only for faces with no stated intent**: a protected face is a violation the tool layer rejects (ADR 0002 Amendment 1, consequences) |
| `set_depth` on G to depth d | Update the `depth` parameter of the blind extrude that made G's floor: new depth = old depth − (deepest − d) along the cut direction | Feature-level, partial update | The update path is verified-live. The parameter ID `depth` is prior; `featurespecs` confirms it in one read. The sign of the arithmetic is inference |
| `set_depth` on a through-all or up-to-face extrude | Change the end type to blind first | Feature-level, but two parameters | Inference. **Out of scope for the demo** |
| `set_min_radius` on G to r, where the corners come from a **fillet feature** | Update that fillet's `radius` | Feature-level | The update path is verified-live. The parameter ID `radius` was accepted on an *add* in Spike 0001 (verified-live); on an update it's inference |
| `set_min_radius` where the corners are **sketch fillets** (the demo pockets' r6) | Update the sketch's radius constraint, sending the whole sketch back | Feature-level (the sketch is a feature) | **Inference by analogy.** Spike 0001 changed sketch `DIAMETER` constraints this way; a radius constraint hasn't been tried |
| `set_min_radius` where the corners are **sharp** | **Add** a fillet feature on the concave edges | Feature-level, but needs **edge** IDs | Adding a fillet with `entities = [edge IDs]` is verified-live (Spike 0001). Mapping our `Edge`s to Onshape edge IDs needs the FS call extended to `EntityType.EDGE` (inference) |
| `add_tool`, `add_fixture`, `route_machine`, back-chamfer | None: shop-side edits | — | — |

**No edit needs a FeatureScript *feature*.** FeatureScript is only read (the mapping call). Every write is a single-feature REST update.

**Muse Code's role (locked decision 5):**
- **Muse Code does:** given the feature's JSON (from the feature list), the `featurespecs` for its type and the edit intent (`set_depth G12 → 6 mm`), it writes the patched feature JSON.
- **The harness does:**
  - checks that the patch changes only the intended parameter;
  - sends it with `rejectMicroversionSkew`;
  - reads `featureState` from the response;
  - **re-counts setups from the branch's STEP**, which is the proof (ADR 0002 decision 4).
- **A patch that fails any check is never resent blindly.** The comment reports the predicted result as unproven (below).

## Minimum live proof: 7–8 calls

The proof tests the **mechanics**. `remove_feature` on the bottom chamfer predicts 2 setups → 1.
- **Under (c),** with ADR 0002 Amendment 2 accepted (Rayyan, 2026-10-30; PR #35), it can run on the **demo's Part A itself**: the branch it builds is the declined alternative, on an `anville/alt-<run>` branch.
- A **proof copy** whose chamfer is named plainly ("Chamfer 1", no stated intent) stays an option if Part A should stay untouched before the recording.

| # | Call | Calls |
|---|---|---|
| 1 | `POST /documents/d/{did}/workspaces` `{versionId, name: "anville/fix-proof"}` | 1 |
| 2 | `GET .../w/{wid}/e/{eid}/features`: the feature IDs, and the chamfer's full JSON | 1 |
| 3 | `POST .../w/{wid}/e/{eid}/featurescript`: the mapping script, given the IDs from 2 | 1 |
| 4 | `POST .../w/{wid}/e/{eid}/features/featureid/{fid}`: the chamfer's JSON with `suppressed: true`, plus `sourceMicroversion` and `rejectMicroversionSkew: true` | 1 |
| 5 | `POST /partstudios/d/{did}/w/{wid}/e/{eid}/translations` `{formatName: "STEP", storeInDocument: false, notifyUser: false}` | 1 |
| 6 | `GET /translations/{tid}` until done | 1–2 |
| 7 | `GET /documents/d/{did}/externaldata/{resultExternalDataIds[0]}` | 1 |

That's **7–8 calls**. Then, locally and with no calls:
- `extract` + `min_setups` on the downloaded STEP must give **1 setup**, matching `evaluate_edits`' prediction. The same 2 → 1 is already verified-local on a code twin of Part A (`tests/geometry/test_demo_parts.py`).
- The mapping script's output must name the chamfer's feature for our chamfer group, through `pair()`. The feature list's IDs and FeatureScript's `makeId(fid)` must agree, which is what call 3 tests.

**What this settles:**
- the face-group-to-feature mapping;
- that a branch workspace exports STEP;
- that the round trip "predicted 2 → 1, measured 1" holds on real Onshape geometry;
- that the skew guard is accepted.

**What it doesn't settle:**
- a `set_depth` or `set_min_radius` patch written by Muse Code (one more update call each);
- the edge-ID mapping;
- concurrent edits by the designer.

## The demo beat

The brief's demo spine (§7) has "Anville opens a fix branch → setups drop". But on both demo parts, the only part-side edit that lowers setups is removing the bottom chamfer. ADR 0002 Amendment 1 shows radius and depth edits never change setup count; only removal or 5-axis does. And that chamfer carries a stated intent, so Anville must not remove it. The demo's fix is shop-side (the back-chamfer cutter, PRs #15 and #18).

**So, as the parts stand, a fix branch can't make setups drop in the demo.** This needs Rayyan's call, not the geometry session's. Options:
- **(a)** The branch carries a fix that doesn't change setups (a radius or depth edit that lets a larger tool finish a face), and setups drop through the shop-side fix instead.
- **(b)** A third demo part has an intent-free feature whose removal drops a setup.
- **(c)** The beat shows the branch as the *alternative* the designer declines ("or remove the chamfer: 1 setup, but it loses weld clearance").

**Decided by Rayyan (2026-10-30): (c).** The fix branch is the alternative the designer declines. Anville opens a branch with the chamfer suppressed and shows setups 2 → 1 measured on it. It says plainly that this costs the weld clearance, and recommends the shop-side cutter instead. The designer keeps the chamfer, and the branch is never merged.

**What (c) needs, by layer:**
- **Geometry:** nothing new. `evaluate_edits` already predicts the removal (2 → 1; `tests/geometry/test_demo_parts.py`), because protection isn't a geometry rule.
- **Agent layer (orchestrator's lane):** today `anville/agent/review.py` rejects a part edit on a protected group before scoring (ADR 0002 Amendment 1, "rejected, not scored"). (c) needs that rule split in two:
  - a protected-face edit is **never recommended and never merged**;
  - but it **may be shown as a declined alternative**, with its intent cost stated, and **built on a branch for that purpose**.
  ADR 0002 Amendment 2 (accepted by Rayyan 2026-10-30, PR #35) makes this split.
- **Comment:** "flag then suggest" order. Lead with the shop-side fix; the branch comes second, with its cost.

## Fallback, already built

If any step fails, the comment carries a **written fix plus a rendered markup** and is labelled "predicted, not applied":
- **Text:** "Suppress *Chamfer 1*: setups 2 → 1 (predicted from the geometry; Anville couldn't apply it on a branch)."
- **Picture:** the RFC 0004 render of the group with its `marker` (an arrow on a chamfer).

Nothing in the fallback needs a new call or a new model.

## Open questions

- Does `makeId` take the REST `featureId` unchanged (inference)? Call 3 answers it. If not, `lastModifyingOperationId` (prior) returns operation IDs that can be compared to the feature list's.
- `evArea` and `evSurfaceDefinition` are prior. If either is missing, `bodydetails` (RFC 0003) supplies surface type and area, and the FS call returns IDs and boxes only.
- Suppressing a feature that later features reference breaks them. The demo parts put the chamfer last (build sheet) for this reason. On a designer's part, `featureState` after suppression says whether downstream features failed, and a failed rebuild means fall back.
- Should the mapping call replace RFC 0003's `bodydetails` read? One FS call would serve both. To settle when both RFCs are discussed.

## Recommendation

1. Treat the brief's "fix branch feasibility" as **answered yes for edits and suppression** (Spike 0001; ADR 0002 Amendment 1), and **open only for the mapping and the re-count**.
2. Spend the 7–8 calls above on Part A (or a proof copy) once Rayyan says go. If the mapping names the right feature and the STEP re-count matches the prediction, write an ADR for the mapping-call design and build `pair()` into geometry as `match_faces` (RFC 0003).
3. **Demo beat: (c), decided by Rayyan.** ADR 0002 Amendment 2 (accepted 2026-10-30, PR #35) allows the branch as the declined alternative, never recommended or merged.
