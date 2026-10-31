"""Probe for RFC 0003: can Anville's STEP faces be matched to Onshape's face IDs? (geometry session, 2026-10-30)

One Onshape call: GET /partstudios/d/{did}/v/{vid}/e/{eid}/bodydetails, saved raw to --out. Then the
faces of the STEP exported from the SAME version are matched to it by surface type, area and bounding box,
and the script reports what matched, what was ambiguous, and the unit scale it found.

    # main session (spends 1 call):
    uv run python scripts/probe_face_anchoring.py --did D --vid V --eid E --step /path/V.step --out bodydetails.json
    # anyone, no API call (re-match a saved response):
    uv run python scripts/probe_face_anchoring.py --from-json bodydetails.json --step /path/V.step

Read-only: it never posts, comments or edits. Arguments and the STEP file are checked before the call.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Onshape surface types (bodydetails; the 2021 schema lists these, lower case) -> ours (FaceFacts.surface).
# Freeform kinds pair only with our freeform faces. Any other name is reported and never matched.
TYPES = {"plane": "plane", "cylinder": "cylinder", "cone": "cone", "sphere": "sphere", "torus": "torus",
         "revolved": "freeform", "extruded": "freeform", "other": "freeform", "bspline": "freeform"}
FREEFORM = {"bspline", "other"}  # FaceFacts.surface values that a freeform Onshape face may be
SCALES = (1.0, 1000.0, 25.4)     # Onshape value × scale = mm
AREA_TOL = 0.005  # relative
BOX_TOL = 0.05    # mm, worst corner difference for a confident match


def _corner(raw, scale):
    if isinstance(raw, dict) and all(k in raw for k in "xyz"):
        raw = [raw["x"], raw["y"], raw["z"]]
    if isinstance(raw, (list, tuple)) and len(raw) == 3 and all(isinstance(c, (int, float)) for c in raw):
        return [c * scale for c in raw]
    return None


def _box(raw, scale):
    """Onshape's box in mm, or None when its shape isn't one we recognize (the Box schema is unverified)."""
    if not isinstance(raw, dict):
        return None
    for lo, hi in (("minCorner", "maxCorner"), ("min", "max")):
        if lo in raw and hi in raw:
            a, b = _corner(raw[lo], scale), _corner(raw[hi], scale)
            return (a, b) if a and b else None
    if all(k in raw for k in ("lowX", "lowY", "lowZ", "highX", "highY", "highZ")):
        return _corner([raw["lowX"], raw["lowY"], raw["lowZ"]], scale), \
            _corner([raw["highX"], raw["highY"], raw["highZ"]], scale)
    return None


def _kind(face) -> tuple[str | None, str]:
    surface = face.get("surface")
    name = str(surface.get("type")) if isinstance(surface, dict) else "None"
    return TYPES.get(name.lower()), name


def _same_kind(theirs: str, ours: str) -> bool:
    return theirs == ours or (theirs == "freeform" and ours in FREEFORM)


def _onshape_faces(details):
    return [f for body in details.get("bodies", []) for f in body.get("faces", [])]


def _scale(ours: list, theirs: list) -> float:
    """The unit scale under which most of our faces find a face of equal area. Totals would mislead: the
    Part Studio may hold other bodies, and our STEP holds one solid."""
    def hits(s):
        areas = [f.get("area") * s * s for f in theirs if isinstance(f.get("area"), (int, float))]
        return sum(any(abs(a - area) <= AREA_TOL * max(area, 1e-9) for a in areas) for _, _, area, _, _ in ours)
    return max(SCALES, key=lambda s: (hits(s), s == 1000.0))  # ties go to metres (prior: Onshape's REST unit)


def pair(ours: list, theirs: list) -> dict:
    """Match ours [(face_id, surface, area mm², box lo, box hi)] to Onshape faces. Only a box within BOX_TOL
    that beats the runner-up 10× is a match; one Onshape ID claimed twice makes both claims ambiguous."""
    scale = _scale(ours, theirs)
    unknown = sorted({name for f in theirs for kind, name in [_kind(f)] if kind is None})
    rows = []
    for fid, surface, area, lo, hi in ours:
        cands = []
        for t in theirs:
            kind, _ = _kind(t)
            a = t.get("area")
            if kind is None or not _same_kind(kind, surface) or not isinstance(a, (int, float)):
                continue
            if abs(a * scale * scale - area) > AREA_TOL * max(area, 1e-9):
                continue
            box = _box(t.get("box"), scale)
            err = max(abs(x - y) for x, y in zip([*lo, *hi], [*box[0], *box[1]])) if box else math.inf
            cands.append((err, str(t.get("id"))))
        cands.sort()
        best = cands[0] if cands else None
        runner = cands[1][0] if len(cands) > 1 else math.inf
        if best and best[0] <= BOX_TOL and runner > 10 * max(best[0], BOX_TOL / 10):
            verdict = "match"
        elif best and math.isinf(best[0]):
            verdict = "unconfirmed (no box)"  # type and area alone can't place a face: never a match
        else:
            verdict = "ambiguous" if len(cands) > 1 else "unmatched"
        rows.append({"face": fid, "surface": surface, "area_mm2": round(area, 3), "verdict": verdict,
                     "onshape_id": best[1] if verdict == "match" else None,
                     "box_err_mm": None if not best else best[0], "candidates": len(cands)})
    claims = {}
    for r in rows:
        if r["verdict"] == "match":
            claims.setdefault(r["onshape_id"], []).append(r)
    clashes = {oid: [r["face"] for r in rs] for oid, rs in claims.items() if len(rs) > 1}
    for rs in claims.values():
        if len(rs) > 1:
            for r in rs:
                r["verdict"], r["onshape_id"] = "ambiguous", None
    return {"scale_to_mm": scale, "faces_ours": len(ours), "faces_onshape": len(theirs),
            "matched": sum(r["verdict"] == "match" for r in rows), "clashes": clashes,
            "unknown_surface_types": unknown, "rows": rows,
            "sample_face_keys": sorted(theirs[0]) if theirs else [],
            "sample_box": theirs[0].get("box") if theirs else None,
            "sample_surface": theirs[0].get("surface") if theirs else None}


def _ours(step: str) -> list:
    """(face_id, surface, area mm², box lo, box hi) for every face extract() reports."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS

    from anville.geometry import extract
    from anville.geometry.step import ShapeMap, _bbox, _read

    facts = extract(step)
    fmap = ShapeMap()
    TopExp.MapShapes_s(_read(Path(step)), TopAbs_FACE, fmap)
    return [(f.id, f.surface, f.area, *_bbox(TopoDS.Face(fmap.FindKey(f.id)))) for f in facts.faces]


def _client():
    from anville.connectors.onshape.client import OnshapeClient
    return OnshapeClient.from_env(os.environ)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True)
    ap.add_argument("--from-json")
    ap.add_argument("--did")
    ap.add_argument("--vid")
    ap.add_argument("--eid")
    ap.add_argument("--out", default="bodydetails.json")
    args = ap.parse_args(argv)
    if not args.from_json and not (args.did and args.vid and args.eid):
        ap.error("--did, --vid and --eid are all needed for the live call (or pass --from-json)")
    if not Path(args.step).is_file():
        ap.error(f"no STEP file at {args.step}: export it before spending the call")
    ours = _ours(args.step)  # a STEP that won't read fails here, before the call
    if args.from_json:
        details = json.loads(Path(args.from_json).read_text())
    else:
        client = _client()
        details = client.get(f"/partstudios/d/{args.did}/v/{args.vid}/e/{args.eid}/bodydetails")
        Path(args.out).write_text(json.dumps(details, indent=1))
        print(f"saved {args.out}; Onshape calls today: {client.calls_today()}")
    report = pair(ours, _onshape_faces(details))
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=1))
    for r in report["rows"]:
        print(f"  F{r['face']:<3} {r['surface']:9} {r['area_mm2']:>10} mm²  {r['verdict']:<22} "
              f"{r['onshape_id']}  box_err={r['box_err_mm']}  candidates={r['candidates']}")


if __name__ == "__main__":
    main()
