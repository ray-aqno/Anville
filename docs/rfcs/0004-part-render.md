# RFC 0004: A highlighted render of the part

- **Status:** Draft
- **Date:** 2026-10-30
- **Author:** geometry session (spike requested by the main session)
- **Touches:** ADR 0007 (panel), RFC 0003 (face anchoring fallback), locked decision 5 (SAM 3.1's role)

Labels: **verified** means checked, with the source given. **Prior** means from memory. **Inference** means reasoned but not checked.

## Problem

A flag names a face group ("G3, the bottom chamfer"). Three consumers need a picture of it:
1. the panel or comment ("this chamfer");
2. the image SAM 3.1 would segment, if it earns its role (it is first in the cut order, so the picture must stand on its own);
3. a stand-in anchor while RFC 0003's probe waits, and for any face that matches nothing.

## Constraints

- **Headless and deterministic.** Modal has no display. The same STEP and faces must give the same bytes.
- **A few seconds at most.** No new external service. A new Python dependency is allowed, but must be justified.
- It reads the same STEP and face IDs as `extract`.

## Options

| | Renderer | For | Against |
|---|---|---|---|
| **A** | **Pure Python z-buffer over OCP's own mesher (`BRepMesh`), PNG via stdlib `zlib`** | No new dependency; exact occlusion; byte-for-byte deterministic; nothing to install on the image | Slower than C. Flat shading only. |
| B | OCCT's `V3d` viewer, offscreen | The real CAD look | Needs an OpenGL context (EGL or OSMesa), which a display-less container may lack (inference); output depends on the driver; OCP's wheel ships `TKOpenGl`, but not with offscreen tested here (unverified) |
| C | Pillow with the painter's algorithm | A small, common dependency | A new dependency; painter's ordering errs where triangles overlap in depth, as in pockets and holes |
| D | VTK, pyrender or matplotlib | Mature | Heavy dependencies; VTK was left out of the image on purpose (`cadquery-ocp-novtk`); matplotlib's 3D has no true occlusion |

## Decision proposed: A

`anville/geometry/render.py`: `render(step, faces, view=None, size=(800, 600)) -> Render` (verified, `tests/geometry/test_render.py`):
- **Image:** orthographic projection, flat shading, 2× supersampling. B-rep edges are drawn only over one of their own faces (or the background, at the silhouette) and at that face's depth, so edges under a thin skin stay hidden.
- **Highlight:** the group in orange (255, 106, 0). Inside the group, blue stays 0 under shading. At its border, supersampling mixes in the neighbours, so key on **R > B** instead: the part, the edges and the background all have R < B.
- **Camera:** with no `view`, it goes to the isometric corner that shows the **most of the group**, counted on a 160×120 pass of all eight corners. It falls back to the six axis views only when no corner shows any of it. Part A's bottom chamfer (G3) is therefore seen from below.
- **`Render`** also carries `view`, `highlighted` and `project(point)`, so a caption or arrow can be placed. `highlighted` counts the pixels where any of the group shows after edges are drawn; 0 means hidden from this view, or too small to show at this size.
- **`box`, `pieces` and `marker`:**
  - `box` is the pixel extent of the group as it shows, or `None`. It is not the projected geometry: a through-hole's hidden 20 mm bore would stretch a geometric box from about 52 px to 132 px.
  - `pieces` lists each connected visible piece as (box, pixels), largest first. Occlusion can split a group; Part A's pocket walls show as 5 pieces.
  - `marker` reads the largest piece **relative to the part on screen (`part_box`)**, not the canvas, because a wider canvas doesn't draw a height-limited part any bigger. Thresholds were measured on all 39 groups of Part A, Part B and the Onshape bracket:
    - `"ring"`: the piece's long side is under **0.18** of the part's long side. Every hole is at most 0.122; everything else is at least 0.236.
    - `"arrow"`: the piece fills under **20%** of its box **and** covers under **4%** of the part's box. Chamfer stripes fill 4–13% and cover at most 1.8%; big faces seen at an angle fill 16% but cover 8%.
    - `None` for everything else, including a group that doesn't show.
  - **Result on the three parts:** every hole gets a ring, every chamfer an arrow (including the demo flag, Part A's G3), and nothing else gets either.
- **Refusals:** unknown face IDs, or a group face the mesher gave no triangles, raise `ValueError`, so the caller falls back to words instead of a picture that silently leaves the face out.
- **Timing (verified, local):** 0.99 s for Part A, 0.94 s for the Onshape bracket, at 800×600. Modal's CPU speed relative to this machine is unmeasured.

## Tests

The tests check by a second route:
- G3's B-rep vertices, projected through the render's camera, must enclose every orange pixel decoded from the PNG, and those pixels must fill more than 70% of that polygon.
- Same input gives the same bytes.
- A hidden group reports 0 and shows no orange.
- Unknown face IDs are refused.
- From the Codex review, each failing before its fix:
  - edges under a 0.02 mm skin stay hidden;
  - `highlighted` equals the number of orange-tinted pixels as Part A shrinks to 12×9;
  - the bottom face's holes stay unlit, seen straight from below (this also caught a crash on axis views);
  - an unmeshed group face is refused.

Nine deliberate bugs each fail at least one test: the four above reverted, plus off-by-one face IDs, flipped culling, a flipped `project`, disabled view picking, and edges that don't clear the highlight.

The marker has its own tests: the groups nearest each cutoff on real parts, one pixel either side of each cutoff on synthetic pieces, two far-apart pieces (two rings, not one arrow), and a wider canvas that leaves the marker unchanged. Moving any threshold across its measured gap fails at least one real-part test, and so does reverting to the union box or to the canvas.

## Open questions

- **Small features:** the bracket's Ø9.5 hole is about 600 pixels at 800×600. `marker` says which overlay it needs; drawing the ring or arrow belongs to the job and panel layer (the orchestrator).
- **The marker thresholds** come from three parts. A part much larger or smaller than these, or a feature type not seen here (a slot, a boss), may need them revisited.
- **Curved faces are shaded flat,** so faceting shows on bores. Smooth shading would need per-vertex normals; that's cosmetic.
- **Whether SAM 3.1 earns its role over this.** The render already knows exactly which pixels are the group. SAM's value would be on images Anville didn't draw, such as drawings and shop photos, and that path (ADR 0005) was rejected and deferred past the event, per the orchestrator. For the event, this render and SAM on it are the only visual path. Inference.
