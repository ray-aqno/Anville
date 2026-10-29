# Demo parts: Onshape build sheet

Two synthetic brackets for the demo (locked decision 7). They're designed to *look like* Baja hardware, and are built fresh during the event: no CAD from before Oct 26, no Eagle Racing geometry. Each one has a matching part in code, in `tests/geometry/parts/__init__.py`, with tests that assert the results below. If an Onshape export disagrees with its code twin, the difference is an Onshape export quirk or an extraction bug, not the part.

**Conventions for both parts:**
- **Units:** mm. Material: 6061-T6 aluminum (appearance only; the planner doesn't read material).
- **Orientation:** model with Z up and the part's corner at the origin, on the Top plane. The setup directions in the expected results (+Z, −Z) assume this orientation.
- **One part per Part Studio.** `extract` rejects a STEP file that holds more than one solid.
- **The bottom chamfer is its own feature, and the last one.** Nothing downstream depends on it, so suppressing it can never break a reference.

Onshape feature names and dialogs below are from memory, not checked against the current UI.

## Part A: shock-mount bracket (the first review)

| # | Feature | Definition |
|---|---|---|
| 1 | Extrude `Block` | Sketch on Top: corner rectangle (0, 0) to (100, 60). Extrude 20 mm up. |
| 2 | Extrude `Pocket` (remove) | Sketch on the top face: rectangle (25, 15) to (75, 45), four sketch fillets of r6. Remove 8 mm, blind. |
| 3 | Hole or Extrude `Mount holes` (remove) | Two Ø8.5 circles (M8 clearance) at (12, 30) and (88, 30). Through all. |
| 4 | Chamfer `Edge break` | Equal distance, 1 mm, on the **top** long edge at y = 60. |
| 5 | Chamfer `Weld clearance chamfer` | Equal distance, 1.5 mm, on the **bottom** long edge at y = 0. **Last feature.** |

**Expected Anville results** (shop: EM12, EM6, EM3 flat endmills at L:D 4):
- **2 setups: +Z, then −Z.** The weld-clearance chamfer is the *only* face in the −Z setup.
- The 1 mm top edge break costs nothing: it is cut in the +Z setup.
- **The fix is shop-side.** The feature's name states its intent, so the face is protected and Anville must not propose removing it. The demo fix is a back-chamfer cutter that reaches the bottom edge from +Z: **1 setup, intent intact.** This needs tool types in the shop profile (an RFC is pending), so it isn't modelled yet.
- On 3+2 (5-axis): 1 setup, another shop-side route.
- `remove_feature` still computes the removal as a capability: predicted 2 → 1, matching re-extraction of the part with feature 5 suppressed (`test_demo_parts.py`).
- Finishing tools: pocket walls, floor and r6 corners EM12; holes EM6; chamfers EM12.
- Extraction: about 0.8 s locally (19 faces).

**The story:** the designer chamfered the bottom edge to clear the weld bead where the bracket sits on the frame tube. That one 1.5 mm chamfer is the only reason to flip the part.

## Part B: tab plate (the second-part learning beat)

| # | Feature | Definition |
|---|---|---|
| 1 | Extrude `Block` | Sketch on Top: corner rectangle (0, 0) to (80, 40). Extrude 20 mm up. |
| 2 | Extrude `Pocket` (remove) | Sketch on the top face: rectangle (30, 10) to (70, 30), four sketch fillets of r5. Remove 8 mm, blind. |
| 3 | Hole or Extrude `Mount hole` (remove) | One Ø10.5 circle (M10 clearance) at (15, 20). Through all. |
| 4 | Chamfer `Weld clearance chamfer` | Equal distance, 1.5 mm, on the **bottom** short edge at x = 0. **Last feature.** |

**Expected Anville results:**
- **2 setups: +Z, then −Z.** The chamfer is again the only −Z face.
- The same shop-side fix applies. As a capability, removal computes 2 → 1, predicted and measured.
- Finishing tools: everything EM6 (the r5 corners cap the tool at Ø10).

**For the learning beat:** this part arrives with the same pattern, a bottom chamfer that alone costs a flip. Whatever Anville learned on Part A (the designer's reason for the chamfer, or the shop's note on bottom edge breaks) should shape this review from the start. Which lesson to record is the main session's call.

## Keep the parts within the model

- **Through holes must stay within a fitting endmill's reach (4×D).** Shop tools have no *type* yet, so there are no drills. Part B was thinned from 25 mm to 20 mm because a 25 mm deep Ø10.5 hole came out unreachable: EM6 reaches only 24 mm. A drill entry with a higher L:D would fix the hole, but the planner would also let that "drill" finish pocket floors.
- **Keep bottom chamfers at 1.5 mm.** The back-chamfer cutter (Harvey 910480) reaches 2.32 mm past its neck, so 1.5 mm leaves 0.82 mm between the neck and the wall. At 2 mm the gap would be 0.32 mm.
- **Keep each chamfer to one edge.** Suppression is all-or-nothing per CAD feature (ADR 0002 Amendment 1).
- **The tools are generic flat endmills.** The plan says EM12 for a 45° chamfer face. A real shop would use a chamfer mill. The setup count, which is what the demo shows, doesn't depend on this.
