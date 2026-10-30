"""Probe for RFC 0003: can Anville's STEP faces be matched to Onshape's face IDs? (geometry session, 2026-10-30)

One Onshape call: GET /partstudios/d/{did}/v/{vid}/e/{eid}/bodydetails, saved raw to --out. Then the
faces of the STEP exported from the SAME version are matched to it by surface type, area and bounding box,
and the script reports what matched, what was ambiguous, and the unit scale it found.

    # main session (spends 1 call):
    uv run python scripts/probe_face_anchoring.py --did D --vid V --eid E --step /path/V.step --out bodydetails.json
    # anyone, no API call (re-match a saved response):
    uv run python scripts/probe_face_anchoring.py --from-json bodydetails.json --step /path/V.step

Read-only: it never posts, comments or edits.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402

from anville.geometry import extract  # noqa: E402
from anville.geometry.step import ShapeMap, _bbox, _read  # noqa: E402

# Onshape surface types (bodydetails) -> ours (FaceFacts.surface). Unlisted Onshape types match "bspline"/"other".
TYPES = {"plane": "plane", "cylinder": "cylinder", "cone": "cone", "sphere": "sphere", "torus": "torus"}
AREA_TOL = 0.005  # relative
BOX_TOL = 0.05    # mm, worst corner difference for a confident match


def _box(raw, scale):
    """Onshape's box, in mm, whatever its key names turn out to be (the schema's Box fields are unverified)."""
    if not isinstance(raw, dict):
        return None
    for lo, hi in (("minCorner", "maxCorner"), ("min", "max")):
        if lo in raw and hi in raw:
            return [c * scale for c in raw[lo]], [c * scale for c in raw[hi]]
    if all(k in raw for k in ("lowX", "lowY", "lowZ", "highX", "highY", "highZ")):
        return ([raw["lowX"] * scale, raw["lowY"] * scale, raw["lowZ"] * scale],
                [raw["highX"] * scale, raw["highY"] * scale, raw["highZ"] * scale])
    return None


def _onshape_faces(details):
    return [f for body in details.get("bodies", []) for f in body.get("faces", [])]


def match(details: dict, step: str) -> dict:
    facts = extract(step)
    shape = _read(Path(step))
    fmap = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    ours = {f.id: (f, _bbox(TopoDS.Face(fmap.FindKey(f.id)))) for f in facts.faces}
    theirs = _onshape_faces(details)

    # Units: Onshape's REST API reports metres (prior). Take the scale from total area instead of assuming it.
    area_ours = sum(f.area for f, _ in ours.values())
    area_theirs = sum(f.get("area", 0.0) for f in theirs)
    scale = math.sqrt(area_ours / area_theirs) if area_theirs else float("nan")
    scale = min((1.0, 1000.0, 25.4), key=lambda s: abs(math.log(s / scale))) if area_theirs else 1000.0

    rows, used = [], {}
    for fid, (f, (lo, hi)) in sorted(ours.items()):
        cands = []
        for t in theirs:
            kind = TYPES.get(str(t.get("surface", {}).get("type", "")).lower(), "bspline")
            if kind != f.surface and not (kind == "bspline" and f.surface in ("bspline", "other")):
                continue
            a = t.get("area", 0.0) * scale * scale
            if abs(a - f.area) > AREA_TOL * max(f.area, 1e-9):
                continue
            box = _box(t.get("box"), scale)
            err = max(abs(x - y) for x, y in zip([*lo, *hi], [*box[0], *box[1]])) if box else math.inf
            cands.append((err, t["id"]))
        cands.sort()
        best = cands[0] if cands else None
        runner = cands[1][0] if len(cands) > 1 else math.inf
        if best and best[0] <= BOX_TOL and runner > 10 * max(best[0], BOX_TOL / 10):
            verdict = "match"
        elif best and math.isinf(best[0]) and len(cands) == 1:
            verdict = "match (area + type only, no box)"
        else:
            verdict = "ambiguous" if len(cands) > 1 else "unmatched"
        if best and verdict.startswith("match"):
            used.setdefault(best[1], []).append(fid)
        rows.append({"face": fid, "surface": f.surface, "area_mm2": round(f.area, 3), "verdict": verdict,
                     "onshape_id": best[1] if best else None, "box_err_mm": None if not best else best[0],
                     "candidates": len(cands)})
    clashes = {oid: fids for oid, fids in used.items() if len(fids) > 1}
    return {"scale_to_mm": scale, "faces_ours": len(ours), "faces_onshape": len(theirs),
            "matched": sum(r["verdict"].startswith("match") for r in rows) - sum(len(v) for v in clashes.values()),
            "clashes": clashes, "rows": rows,
            "sample_face_keys": sorted(theirs[0]) if theirs else [],
            "sample_box": theirs[0].get("box") if theirs else None,
            "sample_surface": theirs[0].get("surface") if theirs else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True)
    ap.add_argument("--from-json")
    ap.add_argument("--did")
    ap.add_argument("--vid")
    ap.add_argument("--eid")
    ap.add_argument("--out", default="bodydetails.json")
    args = ap.parse_args()
    if args.from_json:
        details = json.loads(Path(args.from_json).read_text())
    else:
        from anville.connectors.onshape.client import OnshapeClient
        client = OnshapeClient.from_env(os.environ)
        details = client.get(f"/partstudios/d/{args.did}/v/{args.vid}/e/{args.eid}/bodydetails")
        Path(args.out).write_text(json.dumps(details, indent=1))
        print(f"saved {args.out}; Onshape calls today: {client.calls_today()}")
    report = match(details, args.step)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=1))
    for r in report["rows"]:
        print(f"  F{r['face']:<3} {r['surface']:9} {r['area_mm2']:>10} mm²  {r['verdict']:<32} "
              f"{r['onshape_id']}  box_err={r['box_err_mm']}  candidates={r['candidates']}")


if __name__ == "__main__":
    main()
