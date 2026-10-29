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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Transform
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps

from anville.geometry.facts import VECTORS, EdgeChamfer, PartFacts
from anville.geometry.shop import BackChamferTool, ShopProfile, SoftJaws
from anville.geometry.step import _bbox, _read

MIN_NECK_CLEARANCE = 0.5  # mm (check c)
NECK_REACH_MARGIN = 5.0   # mm above stock height (check d)
ANGLE_TOL = 0.5           # degrees (check a)
RETRACT = 0.01            # mm off the chamfer, so nominal contact doesn't read as a collision
COLLISION = 1e-3          # mm³ of overlap that counts as a collision

Vec = tuple[float, float, float]


@dataclass
class Check:
    name: str            # a-f, as in RFC 0002 §2
    passed: bool
    margin: float | None  # mm (degrees for a); positive = room to spare
    detail: str


@dataclass
class Relief:
    """A pocket to cut in one soft jaw, derived from the swept cutter plus the margin (for the setup sheet)."""

    jaw: str                # "start" | "end" of the chamfered edge
    origin: Vec             # mm, the pocket's minimum corner in part coordinates
    size: Vec               # mm along X, Y, Z
    contact_kept: float     # share of the jaw's grip on the part that the pocket leaves


@dataclass
class BackChamferAccess:
    face_id: int
    tool: str
    checks: list[Check]
    reliefs: list[Relief] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


def assess_back_chamfer(step_path: str | Path, facts: PartFacts, shop: ShopProfile) -> list[BackChamferAccess]:
    """Checks a-f for every recognized exterior edge chamfer and every back-chamfer cutter in the shop."""
    if not facts.edge_chamfers or not shop.back_chamfer_tools:
        return []
    part = _read(Path(step_path))
    return [_assess(part, facts, ch, tool, shop.fixture)
            for ch in facts.edge_chamfers for tool in shop.back_chamfer_tools]


def _assess(part, facts: PartFacts, ch: EdgeChamfer, tool: BackChamferTool, fixture: SoftJaws | None):
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

    overtravel = R if fixture is None or fixture.overtravel is None else fixture.overtravel
    head, neck = _swept_cutter(ch, tool, overtravel)
    checks.append(_check_part(part, head, neck))
    check_f, reliefs = _check_workholding(head, neck, ch, facts, tool, fixture)
    checks.append(check_f)
    return BackChamferAccess(ch.face_id, tool.name, checks, reliefs)


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


def _check_workholding(head, neck, ch: EdgeChamfer, facts: PartFacts, tool: BackChamferTool,
                       fixture: SoftJaws | None) -> tuple[Check, list[Relief]]:
    """Check f: the swept solids against the jaws (relieved from the swept cutter) and the parallel."""
    if fixture is None:
        return Check("f", False, None, "no fixture stated: the one-setup plan needs relieved soft jaws"), []
    e, o, _ = _frame(ch)
    lo, hi = facts.bbox
    ea = 0 if abs(e[0]) > 0.5 else 1          # index of the edge axis
    oa = 1 - ea                                # index of the outward axis
    floor = lo[2] - fixture.parallel_height
    cutter = _fuse(head, neck)

    jaws, reliefs = [], []
    centre = (lo[oa] + hi[oa]) / 2
    for label, (a, b) in (("start", (lo[ea] - fixture.jaw_thickness, lo[ea])),
                          ("end", (hi[ea], hi[ea] + fixture.jaw_thickness))):
        jlo, jhi = [0.0, 0.0, floor], [0.0, 0.0, hi[2]]
        jlo[ea], jhi[ea] = a, b
        jlo[oa], jhi[oa] = centre - fixture.jaw_width / 2, centre + fixture.jaw_width / 2
        jaw = _box(tuple(jlo), tuple(jhi))
        if fixture.relieved and _overlap(cutter, jaw) > COLLISION:
            rlo, rhi = _bbox(BRepAlgoAPI_Common(cutter, jaw).Shape())
            rlo = tuple(max(v - fixture.relief_margin, j) for v, j in zip(rlo, jlo))
            rhi = tuple(min(v + fixture.relief_margin, j) for v, j in zip(rhi, jhi))
            jaw = BRepAlgoAPI_Cut(jaw, _box(rlo, rhi)).Shape()
            reliefs.append(Relief(label, tuple(round(v, 3) for v in rlo),
                                  tuple(round(b - a, 3) for a, b in zip(rlo, rhi)),
                                  round(_contact_kept(rlo, rhi, lo, hi, oa), 3)))
        jaws.append(jaw)

    side = ch.start[oa] + o[oa] * ch.leg       # the chamfered side face's plane, along the outward axis
    setback = tool.head_diameter / 2 + 1.0 if fixture.parallel_setback is None else fixture.parallel_setback
    inner = side - o[oa] * setback             # the parallel's face toward the chamfered side
    plo, phi = list(lo), list(hi)
    plo[2], phi[2] = floor, lo[2]
    plo[oa], phi[oa] = sorted((inner, inner - o[oa] * fixture.parallel_width))
    parallel = _box(tuple(plo), tuple(phi))

    problems = [f"{n} overlaps the swept cutter by {v:.3g} mm³"
                for n, v in (("start jaw", _overlap(cutter, jaws[0])), ("end jaw", _overlap(cutter, jaws[1])),
                             ("parallel", _overlap(cutter, parallel))) if v > COLLISION]
    if problems:
        return Check("f", False, None, "; ".join(problems)), reliefs
    kept = min((rl.contact_kept for rl in reliefs), default=1.0)
    what = "relieved jaws" if reliefs else "jaws"
    return Check("f", True, None, f"clear of {what} and the parallel; reliefs keep {kept:.0%} of the jaw grip"), reliefs


def _contact_kept(rlo, rhi, lo, hi, oa) -> float:
    """Share of a jaw's grip face (part width × part height) that the relief pocket doesn't cut away."""
    width, height = hi[oa] - lo[oa], hi[2] - lo[2]
    cut_w = max(0.0, min(rhi[oa], hi[oa]) - max(rlo[oa], lo[oa]))
    cut_h = max(0.0, min(rhi[2], hi[2]) - max(rlo[2], lo[2]))
    return 1.0 - (cut_w * cut_h) / (width * height)
