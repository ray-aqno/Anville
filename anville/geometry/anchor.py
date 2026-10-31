"""Match our STEP faces to a CAD system's faces (RFC 0003; RFC 0005's face group -> feature map).

Pure Python, two matchers; both match a face only when it beats every other candidate by 10x, so an
anchor is never guessed:
- `pair`: by surface type, area and box (RFC 0003's `bodydetails` probe);
- `match_centroids`: by area and centroid (RFC 0005's FeatureScript call, verified live 2026-10-31), after
  finding the offset between the two frames, so a part modelled off the origin still matches.
"""

from __future__ import annotations

import math

from anville.geometry.facts import PartFacts

# Onshape surface types (bodydetails: lower case; FeatureScript's SurfaceType: upper case) -> ours
# (FaceFacts.surface). Freeform kinds pair only with our freeform faces. Any other name is reported and
# never matched.
TYPES = {"plane": "plane", "cylinder": "cylinder", "cone": "cone", "sphere": "sphere", "torus": "torus",
         "revolved": "freeform", "extruded": "freeform", "other": "freeform", "bspline": "freeform"}
FREEFORM = {"bspline", "other"}  # FaceFacts.surface values that a freeform Onshape face may be
SCALES = (1.0, 1000.0, 25.4)     # their value x scale = mm
AREA_TOL = 0.005  # relative
BOX_TOL = 0.05    # mm, worst corner difference for a confident match


def _corner(raw, scale):
    if isinstance(raw, dict) and all(k in raw for k in "xyz"):
        raw = [raw["x"], raw["y"], raw["z"]]
    if isinstance(raw, (list, tuple)) and len(raw) == 3 and all(isinstance(c, (int, float)) for c in raw):
        return [c * scale for c in raw]
    return None


def _box(raw, scale):
    """Their box in mm, or None when its shape isn't one we recognize."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        a, b = _corner(raw[0], scale), _corner(raw[1], scale)
        return (a, b) if a and b else None
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
    name = str(surface.get("type")) if isinstance(surface, dict) else str(surface)
    return TYPES.get(name.lower()), name


def _same_kind(theirs: str, ours: str) -> bool:
    return theirs == ours or (theirs == "freeform" and ours in FREEFORM)


def _scale(ours: list, theirs: list) -> float:
    """The unit scale under which most of our faces find a face of equal area. Totals would mislead: the
    Part Studio may hold other bodies, and our STEP holds one solid."""
    def hits(s):
        areas = [f.get("area") * s * s for f in theirs if isinstance(f.get("area"), (int, float))]
        return sum(any(abs(a - area) <= AREA_TOL * max(area, 1e-9) for a in areas) for _, _, area, _, _ in ours)
    return max(SCALES, key=lambda s: (hits(s), s == 1000.0))  # ties go to metres (prior: Onshape's REST unit)


def pair(ours: list, theirs: list, *, scale: float | None = None) -> dict:
    """Match ours [(face_id, surface, area mm², box lo, box hi)] to their faces. Only a box within BOX_TOL
    that beats the runner-up 10x is a match; one of their IDs claimed twice makes both claims ambiguous.
    `scale` (their unit -> mm) is found from the areas unless given."""
    scale = scale or _scale(ours, theirs)
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
            "unknown_surface_types": unknown, "rows": rows}


CENTROID_TOL = 0.1  # mm: evApproximateCentroid is exact on planes, and these are 10x apart from neighbours


def _equal_area(a: float, b: float) -> bool:
    return abs(a - b) <= AREA_TOL * max(a, 1e-9)


def frame_offset(facts: PartFacts, theirs: list[dict]) -> tuple[tuple, int] | None:
    """(ours − theirs, faces agreeing) for the offset most equal-area pairs agree on, within CENTROID_TOL.
    None when no offset gets at least 3 faces and twice the votes of any offset that disagrees with it."""
    raw = [tuple(a - b for a, b in zip(f.centroid, t["c"])) for f in facts.faces for t in theirs
           if _equal_area(f.area, t["area"])]
    near = lambda u, v: max(abs(a - b) for a, b in zip(u, v)) <= CENTROID_TOL  # noqa: E731
    scored = sorted(((sum(near(o, p) for p in raw), o) for o in raw), reverse=True)
    if not scored or scored[0][0] < 3:
        return None
    n, best = scored[0]
    rival = next((m for m, o in scored if not near(o, best)), 0)
    if n < 2 * rival:
        return None
    agree = [o for o in raw if near(o, best)]
    return tuple(sum(o[i] for o in agree) / len(agree) for i in range(3)), n


def match_centroids(facts: PartFacts, theirs: list[dict], offset: tuple) -> dict[int, dict]:
    """Face id -> {verdict, id, err_mm, candidates} for each of our faces. `theirs`: {id, area mm², c mm}.
    One of their IDs claimed by two of ours makes both ambiguous."""
    rows = {}
    for f in facts.faces:
        cands = sorted((max(abs(a - (b + d)) for a, b, d in zip(f.centroid, t["c"], offset)), str(t["id"]))
                       for t in theirs if _equal_area(f.area, t["area"]))
        best = cands[0] if cands else None
        runner = cands[1][0] if len(cands) > 1 else math.inf
        ok = best and best[0] <= CENTROID_TOL and runner > 10 * max(best[0], CENTROID_TOL / 10)
        rows[f.id] = {"verdict": "match" if ok else ("ambiguous" if len(cands) > 1 else "unmatched"),
                      "id": best[1] if ok else None, "err_mm": best[0] if best else None,
                      "candidates": len(cands)}
    claimed: dict[str, list[int]] = {}
    for fid, r in rows.items():
        if r["verdict"] == "match":
            claimed.setdefault(r["id"], []).append(fid)
    for fids in claimed.values():
        if len(fids) > 1:
            for fid in fids:
                rows[fid].update(verdict="ambiguous", id=None)
    return rows
