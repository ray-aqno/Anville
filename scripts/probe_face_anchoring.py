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
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anville.geometry.anchor import pair as _pair  # noqa: E402  (moved to geometry for RFC 0005)


def _onshape_faces(details):
    return [f for body in details.get("bodies", []) for f in body.get("faces", [])]


def pair(ours: list, theirs: list) -> dict:
    """anchor.pair, plus a sample of Onshape's face shape: the bodydetails schema is unverified."""
    return {**_pair(ours, theirs),
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
