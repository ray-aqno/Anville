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
- **The fix is shop-side.** The feature's name states its intent, so the face is protected and Anville must not propose removing it. The demo fix is a back-chamfer cutter that reaches the bottom edge from +Z: **1 setup, intent intact.** It is modelled by RFC 0002 (PR #15). With Harvey 910480 and relieved soft jaws, checks a–f all pass (neck clearance 0.825 mm), and the plan is 1 setup. A shop that doesn't own the cutter is offered buying it (PR #18).
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
- The same shop-side fix applies: with the 910480 and relieved jaws, 1 setup (+Z), keeping 97% of the jaw grip. As a capability, removal computes 2 → 1, predicted and measured.
- Finishing tools: everything EM6 (the r5 corners cap the tool at Ø10).

**For the learning beat:** this part arrives with the same pattern, a bottom chamfer that alone costs a flip. Whatever Anville learned on Part A (the designer's reason for the chamfer, or the shop's note on bottom edge breaks) should shape this review from the start. Which lesson to record is the main session's call.

## Part B for the recording: suspension tube insert (Rev B, 2026-11-02)

Replaces the tab plate on camera, so the second part is visibly a different part. It's a flanged insert: the round spigot presses into a suspension tube, the bore takes a bushing, and the flange bolts to a tab. Feature names are neutral; the chamfer's purpose is not named. Unlike the parts above, it is **centred on the origin** (as Rayyan modelled DEMO A). The drawing and recipe are at https://claude.ai/artifact/AsgHSJeTPBDk4timbxSnXB. Code twin: `demo_insert` (tests: `tests/geometry/test_demo_insert.py`).

| # | Feature | Definition |
|---|---|---|
| 1 | Extrude `Flange` | Sketch on Top: centre-point rectangle on the origin, 70 (X) × 40 (Y). Extrude 12 mm up. |
| 2 | Extrude `Spigot` (add) | Sketch on the flange top: Ø32 circle on the origin. Extrude 10 mm up. Part is 22 tall. |
| 3 | Extrude `Bore` (remove) | Sketch on the spigot top: Ø20 circle on the origin. Through all. |
| 4 | Extrude `Mount holes` (remove) | Sketch on the flange top: two Ø6.5 circles (M6 clearance) at (−25, 0) and (25, 0). Through all. |
| 5 | Chamfer `Chamfer 1` | Equal distance, 1 mm, on the **bottom** long edge at Y = +20 (the back). **Last feature.** |

Check in Onshape: Mass properties should show a volume of about 33,900 mm³ (33,899.6).

**Expected Anville results**, on the live review shop (`review_shop`):
- **Fresh shop: 2 setups, +Z then −Z.** Its one issue is the chamfer forcing its own setup. Nothing is unreachable.
- **Once the shop owns the 910480: 1 setup**, no issues. This is the learning beat. **The shop owns it only after a machinist's note on Part A's setup sheet ticks the 910480 as arrived** (`_on_shop_note`). The designer's reply on Part A writes no acquisition, so without that note Part B still reviews at 2 setups. Record the note before saving Part B.
- Checks a–f pass for the 910480: neck clearance 1.325 mm, and the relieved jaws keep 95% of the grip.
- The options have the same shape as Part A's: remove the chamfer 2 → 1, which is also the fix-branch alternative and measures 1 on the twin without it; buy the 910480 2 → 1; a long EM12 from ±X stays at 2.
- In plain words: "the 1 mm chamfer on the bottom back edge" and "the Ø20 through hole".
- **Why 22 tall:** at a 15 mm flange (25 tall) the Ø6.5 holes are past EM6's 24 mm reach from the top, so they forced the flip too, and the chamfer was no longer the only issue.

## Keep the parts within the model

- **Through holes must stay within a fitting endmill's reach (4×D).** Shop tools have no *type* yet, so there are no drills. Part B was thinned from 25 mm to 20 mm because a 25 mm deep Ø10.5 hole came out unreachable: EM6 reaches only 24 mm. A drill entry with a higher L:D would fix the hole, but the planner would also let that "drill" finish pocket floors.
- **Keep bottom chamfers at 1.5 mm or less.** The back-chamfer cutter (Harvey 910480) reaches 2.32 mm past its neck, so 1.5 mm leaves 0.82 mm between the neck and the wall, and the insert's 1 mm leaves 1.325 mm. At 2 mm the gap would be 0.32 mm.
- **Keep each chamfer to one edge.** Suppression is all-or-nothing per CAD feature (ADR 0002 Amendment 1).
- **The tools are generic flat endmills.** The plan says EM12 for a 45° chamfer face. A real shop would use a chamfer mill. The setup count, which is what the demo shows, doesn't depend on this.
