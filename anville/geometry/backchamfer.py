"""assess_back_chamfer: RFC 0002 §2-3, checks a-f for finishing an exterior edge chamfer from +Z.

The cutter's 45° cone lies on the chamfer, so its axis sits one head radius outward from the
chamfer's bottom edge and its widest point is level with the stock bottom. The head and the neck
are each swept along the edge, overtravel included. Sweeping a convex solid of revolution along a
line gives the prism of its profile joined with the solid at both ends: the RFC's "prism with
rounded ends". The checks prove nominal clearance from catalog sizes; the setup sheet still asks
the machinist to verify the toolpath.

Every check is evaluated and reported with its margin, so a failure says which check and by how much.
"""

import math
from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Transform
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps

# The result types live in access.py, free of OCP, so the review job can rebuild them.
from anville.geometry.access import BackChamferAccess, Check, Relief  # noqa: F401  (re-exported)
from anville.geometry.facts import VECTORS, EdgeChamfer, PartFacts
from anville.geometry.shop import BackChamferTool, ShopProfile, SoftJaws
from anville.geometry.step import _bbox, _read

MIN_NECK_CLEARANCE = 0.5  # mm (check c)
NECK_REACH_MARGIN = 5.0   # mm above stock height (check d)
ANGLE_TOL = 0.5           # degrees (check a)
RETRACT = 0.01            # mm off the chamfer, so nominal contact doesn't read as a collision
COLLISION = 1e-3          # mm³ of overlap that counts as a collision

Vec = tuple[float, float, float]


def assess_back_chamfer(step_path: str | Path, facts: PartFacts, shop: ShopProfile) -> list[BackChamferAccess]:
    """Checks a-f for every recognized exterior edge chamfer and every back-chamfer cutter in the shop."""
    if not facts.edge_chamfers or not shop.back_chamfer_tools:
        return []
    part = _read(Path(step_path))
    out = []
    for tool in shop.back_chamfer_tools:
        # Chamfers along one edge axis share one jaw orientation, so check f sees all of them in one fixture.
        for axis in sorted({ch.axis for ch in facts.edge_chamfers}):
            group = [ch for ch in facts.edge_chamfers if ch.axis == axis]
            out += _assess_group(part, facts, group, tool, shop.fixture)
    return out


def _assess_group(part, facts: PartFacts, group: list[EdgeChamfer], tool: BackChamferTool,
                  fixture: SoftJaws | None) -> list[BackChamferAccess]:
    overtravel = tool.head_diameter / 2 if fixture is None or fixture.overtravel is None else fixture.overtravel
    rows = []
    for ch in group:
        checks = _sizes(ch, tool, facts)
        head, neck = _swept_cutter(ch, tool, overtravel)
        checks.append(_check_part(part, head, neck))
        rows.append((ch, checks, _fuse(head, neck)))
    check_f, reliefs = _check_workholding([cutter for _, _, cutter in rows], group, facts, tool, fixture)
    return [BackChamferAccess(ch.face_id, tool.name, checks + [check_f], reliefs) for ch, checks, _ in rows]


def _sizes(ch: EdgeChamfer, tool: BackChamferTool, facts: PartFacts) -> list[Check]:
    """Checks a-d: the cutter's catalog sizes against the chamfer and the stock."""
    R, r = tool.head_diameter / 2, tool.neck_diameter / 2
    lo, hi = facts.bbox
    height = hi[2] - lo[2]
    checks = []

    off = abs(tool.angle - ch.angle)
    checks.append(Check("a", off <= ANGLE_TOL, round(ANGLE_TOL - off, 3),
                        f"cutter {tool.angle:g}° vs chamfer {ch.angle:g}°"))
    room = tool.length_of_cut - ch.leg
    checks.append(Check("b", room >= 0, round(room, 3),
                        f"leg {ch.leg:g} mm vs length of cut {tool.length_of_cut:g} mm"))
    gap = R - r - ch.leg
    short = MIN_NECK_CLEARANCE - gap
    checks.append(Check("c", gap >= MIN_NECK_CLEARANCE, round(gap, 3),
                        f"neck clears the wall by {gap:.3f} mm" + (f", short of {MIN_NECK_CLEARANCE} mm by {short:.3f} mm"
                                                                   if short > 0 else "")))
    need = height + NECK_REACH_MARGIN
    checks.append(Check("d", tool.neck_length >= need, round(tool.neck_length - need, 3),
                        f"neck length {tool.neck_length:g} mm vs stock height + {NECK_REACH_MARGIN:g} = {need:g} mm"))
    return checks


# ---- geometry -------------------------------------------------------------------------------

def _frame(ch: EdgeChamfer):
    """Unit edge direction e, outward o, edge length."""
    d = tuple(b - a for a, b in zip(ch.start, ch.end))
    length = math.sqrt(sum(c * c for c in d))
    return tuple(c / length for c in d), VECTORS[ch.outward], length


def _at(p: Vec, *terms) -> Vec:
    x, y, z = p
    for vec, s in terms:
        x, y, z = x + s * vec[0], y + s * vec[1], z + s * vec[2]
    return (x, y, z)


def _swept_cutter(ch: EdgeChamfer, tool: BackChamferTool, overtravel: float):
    """The head (flat + cone) and the neck, each swept from `overtravel` before the edge to after it."""
    e, o, length = _frame(ch)
    R, r = tool.head_diameter / 2, tool.neck_diameter / 2
    cone = R - r
    up = (0.0, 0.0, 1.0)
    away = tuple(c / math.sqrt(2) for c in (o[0], o[1], -1.0))  # the chamfer's outward normal
    # Axis foot at the stock bottom, one head radius out from the chamfer's bottom edge, backed off RETRACT.
    foot = _at(ch.start, (o, R), (away, RETRACT), (e, -overtravel))
    travel = length + 2 * overtravel

    def head_at(p):
        flat = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*_at(p, (up, -tool.head_flat))), gp_Dir(0, 0, 1)),
                                        R, tool.head_flat).Shape()
        return _fuse(flat, BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(*p), gp_Dir(0, 0, 1)), R, r, cone).Shape())

    def neck_at(p):
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*_at(p, (up, cone))), gp_Dir(0, 0, 1)),
                                        r, tool.neck_length).Shape()

    head_profile = [(-R, -tool.head_flat), (R, -tool.head_flat), (R, 0.0), (r, cone), (-r, cone), (-R, 0.0)]
    neck_profile = [(-r, cone), (r, cone), (r, cone + tool.neck_length), (-r, cone + tool.neck_length)]
    return (_sweep(head_at, head_profile, foot, e, o, travel),
            _sweep(neck_at, neck_profile, foot, e, o, travel))


def _sweep(solid_at, profile, foot: Vec, e: Vec, o: Vec, travel: float):
    """Exact sweep of a convex solid of revolution along e: profile prism joined with both end solids."""
    poly = BRepBuilderAPI_MakePolygon()
    for w, z in profile:
        poly.Add(gp_Pnt(*_at(foot, (o, w), ((0.0, 0.0, 1.0), z))))
    poly.Close()
    prism = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(),
                                  gp_Vec(*(c * travel for c in e))).Shape()
    end = _at(foot, (e, travel))
    return _fuse(_fuse(prism, solid_at(foot)), solid_at(end))


def _fuse(a, b):
    return BRepAlgoAPI_Fuse(a, b).Shape()


def _volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return abs(props.Mass())


def _overlap(a, b) -> float:
    return _volume(BRepAlgoAPI_Common(a, b).Shape())


def _box(lo: Vec, hi: Vec):
    return BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()


# ---- checks e and f -------------------------------------------------------------------------

def _check_part(part, head, neck) -> Check:
    """Check e: the swept head and neck stay clear of the finished part."""
    hits = [(name, _overlap(solid, part)) for name, solid in (("head", head), ("neck", neck))]
    hits = [(n, v) for n, v in hits if v > COLLISION]
    if hits:
        return Check("e", False, None, "; ".join(f"swept {n} cuts {v:.3g} mm³ of the part" for n, v in hits))
    clearance = BRepExtrema_DistShapeShape(neck, part).Value()
    return Check("e", True, round(clearance, 3), f"swept head clear; swept neck clears the part by {clearance:.3f} mm")


def _check_workholding(cutters: list, chamfers: list[EdgeChamfer], facts: PartFacts, tool: BackChamferTool,
                       fixture: SoftJaws | None) -> tuple[Check, list[Relief]]:
    """Check f: every swept cutter of one jaw orientation against one fixture. The jaws are relieved
    for all the cutters, there is one parallel per chamfered side, each parallel must sit under the
    part, and each jaw must keep `min_grip` of its grip."""
    if fixture is None:
        return Check("f", False, None, "no fixture stated: the one-setup plan needs relieved soft jaws"), []
    e, _, _ = _frame(chamfers[0])
    lo, hi = facts.bbox
    ea = 0 if abs(e[0]) > 0.5 else 1          # index of the edge axis (shared by the group)
    oa = 1 - ea                                # index of the axis across it
    names = "XYZ"
    floor = lo[2] - fixture.parallel_height
    problems, reliefs, jaws, kept = [], [], [], {}

    centre = (lo[oa] + hi[oa]) / 2
    for label, (a, b) in (("start", (lo[ea] - fixture.jaw_thickness, lo[ea])),
                          ("end", (hi[ea], hi[ea] + fixture.jaw_thickness))):
        jlo, jhi = [0.0, 0.0, floor], [0.0, 0.0, hi[2]]
        jlo[ea], jhi[ea] = a, b
        jlo[oa], jhi[oa] = centre - fixture.jaw_width / 2, centre + fixture.jaw_width / 2
        jaw = _box(tuple(jlo), tuple(jhi))
        pockets = []
        if fixture.relieved:
            for cutter in cutters:
                if _overlap(cutter, jaw) > COLLISION:
                    rlo, rhi = _bbox(BRepAlgoAPI_Common(cutter, jaw).Shape())
                    pockets.append((tuple(max(v - fixture.relief_margin, j) for v, j in zip(rlo, jlo)),
                                    tuple(min(v + fixture.relief_margin, j) for v, j in zip(rhi, jhi))))
            for rlo, rhi in pockets:
                jaw = BRepAlgoAPI_Cut(jaw, _box(rlo, rhi)).Shape()
        kept[label] = _contact_kept(pockets, lo, hi, oa)
        reliefs += [Relief(label, tuple(round(v, 3) for v in rlo), tuple(round(b - a, 3) for a, b in zip(rlo, rhi)),
                           round(kept[label], 3)) for rlo, rhi in pockets]
        jaws.append((f"{label} jaw", jaw))

    setback = tool.head_diameter / 2 + 1.0 if fixture.parallel_setback is None else fixture.parallel_setback
    parallels = []
    for ch in chamfers:
        o = VECTORS[ch.outward]
        side = ch.start[oa] + o[oa] * ch.leg   # the chamfered side face's plane, along the across axis
        inner = side - o[oa] * setback         # the parallel's face toward that side
        a, b = sorted((inner, inner - o[oa] * fixture.parallel_width))
        if a < lo[oa] - 1e-6 or b > hi[oa] + 1e-6:
            problems.append(f"parallel at {names[oa]} {a:.2f}..{b:.2f} mm is not under the part "
                            f"({names[oa]} {lo[oa]:.2f}..{hi[oa]:.2f} mm)")
        plo, phi = list(lo), list(hi)
        plo[2], phi[2] = floor, lo[2]
        plo[oa], phi[oa] = a, b
        parallels.append(("parallel", _box(tuple(plo), tuple(phi))))

    for name, body in jaws + parallels:
        hit = sum(_overlap(cutter, body) for cutter in cutters)
        if hit > COLLISION:
            problems.append(f"{name} overlaps the swept cutter by {hit:.3g} mm³")
    for label, share in kept.items():
        if share < fixture.min_grip:
            problems.append(f"reliefs leave {share:.0%} of the {label} jaw's grip, below the {fixture.min_grip:.0%} minimum")
    if problems:
        return Check("f", False, None, "; ".join(problems)), reliefs
    what = "relieved jaws" if reliefs else "jaws"
    return Check("f", True, None, f"clear of {what} and {len(parallels)} parallel(s) under the part; "
                                  f"reliefs keep {min(kept.values()):.0%} of the jaw grip"), reliefs


def _contact_kept(pockets, lo, hi, oa) -> float:
    """Share of a jaw's grip face (part width × part height) that its relief pockets leave."""
    rects = []
    for rlo, rhi in pockets:
        a0, a1 = max(rlo[oa], lo[oa]), min(rhi[oa], hi[oa])
        z0, z1 = max(rlo[2], lo[2]), min(rhi[2], hi[2])
        if a1 > a0 and z1 > z0:
            rects.append((a0, a1, z0, z1))
    # Union area of the clipped rectangles, by coordinate compression (a jaw has only a few pockets).
    xs = sorted({v for r in rects for v in r[:2]})
    cut = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        spans = sorted((r[2], r[3]) for r in rects if r[0] <= x0 and r[1] >= x1)
        covered, top = 0.0, -math.inf
        for z0, z1 in spans:
            if z1 > top:
                covered += z1 - max(z0, top)
                top = z1
        cut += (x1 - x0) * covered
    return 1.0 - cut / ((hi[oa] - lo[oa]) * (hi[2] - lo[2]))
