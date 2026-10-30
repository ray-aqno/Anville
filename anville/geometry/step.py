"""STEP -> PartFacts, face-level, by sampled ray casting (ADR 0002 §5, ADR 0008 §1-4).

The only module that imports OCP. Note OCCT 8 API names: TopoDS.Face(s) (a namespace, not
TopoDS.Face_s), static methods end in _s, and shape maps live in OCP.collections.
"""

import hashlib
import itertools
import math
import time
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.collections import (
    IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as EdgeFaceMap,
    IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as ShapeMap,
)
from OCP.GeomAbs import (
    GeomAbs_BSplineSurface, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane,
    GeomAbs_Sphere, GeomAbs_Torus,
)
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt, gp_Pnt2d, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS

from anville.geometry.facts import (
    DIRECTIONS, VECTORS, Access, Edge, EdgeChamfer, FaceFacts, PartFacts, face_groups,
)

SPACING = 2.0            # mm between samples (ADR 0008 §3)
MIN_GRID, MAX_GRID = 8, 32
SLIVER_RETRY = 3         # faces with no interior cell centre retry at this multiple
RAY_OFFSET = 0.01        # mm off the surface before casting
NORMAL_TOL = 0.01        # n·d >= -tol faces the tool (walls parallel to the axis count)
WALL_LIKE = 0.3          # |n projected ⟂ d| above this measures a gap
TANGENT_DOT = math.cos(math.radians(0.5))  # n1·n2 above this: a smooth (tangent) edge; a 5° kink is sharp
PRINCIPAL_DOT = 0.999
STOCK_TOL = 1e-3         # mm
REACH_GRID = 1.0         # mm between height-map rays (ADR 0008 Amendment 1)
REACH_STEP = 0.05        # mm of extra stick-out that starts a new reach step
CHAMFER_ANGLE, CHAMFER_TOL = 45.0, 0.5  # degrees from -Z (RFC 0002 §1)
FAR = 1e9                # mm, ray length

_SURFACES = {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cylinder", GeomAbs_Cone: "cone",
             GeomAbs_Sphere: "sphere", GeomAbs_Torus: "torus", GeomAbs_BSplineSurface: "bspline"}
_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}

Vec = tuple[float, float, float]


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _axpy(a: Vec, b: Vec, s: float = 1.0) -> Vec:
    return (a[0] + s * b[0], a[1] + s * b[1], a[2] + s * b[2])


def _xyz(p) -> Vec:
    return (p.X(), p.Y(), p.Z())


def _principal(v: Vec) -> str | None:
    return next((lab for lab, i in _AXIS_INDEX.items() if abs(v[i]) > PRINCIPAL_DOT), None)


def _bbox(shape) -> tuple[Vec, Vec]:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    return _xyz(box.CornerMin()), _xyz(box.CornerMax())


def _normal(face, surf, u: float, v: float) -> tuple[Vec, Vec] | None:
    """Point and outward unit normal at (u, v), honouring face orientation."""
    p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    surf.D1(u, v, p, du, dv)
    n = du.Crossed(dv)
    if n.Magnitude() < 1e-12:
        return None
    n.Normalize()
    s = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
    return _xyz(p), (s * n.X(), s * n.Y(), s * n.Z())


def _read(path: Path):
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read STEP file: {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    solids = TopExp_Explorer(shape, TopAbs_SOLID)
    count = 0
    while solids.More():
        count += 1
        solids.Next()
    if count != 1:
        raise ValueError(f"expected one solid in {path.name}, found {count}")
    return shape


def _samples(face, extent: float) -> list[tuple[Vec, Vec]]:
    """UV grid cell centres inside the trimmed face, about SPACING apart."""
    surf = BRepAdaptor_Surface(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    tol = BRep_Tool.Tolerance_s(face)
    n = min(MAX_GRID, max(MIN_GRID, math.ceil(extent / SPACING)))
    for grid in (n, n * SLIVER_RETRY):
        out = []
        for i, j in itertools.product(range(grid), range(grid)):
            u = u0 + (i + 0.5) * (u1 - u0) / grid
            v = v0 + (j + 0.5) * (v1 - v0) / grid
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), tol).State() != TopAbs_IN:
                continue
            pn = _normal(face, surf, u, v)
            if pn:
                out.append(pn)
        if out:
            return out
    return []


def _first_hit(isect, p: Vec, d: Vec) -> float:
    isect.Perform(gp_Lin(gp_Pnt(*p), gp_Dir(*d)), 0.0, FAR)
    if not isect.IsDone() or isect.NbPnt() == 0:
        return math.inf
    return min(isect.WParameter(k) for k in range(1, isect.NbPnt() + 1))


def _access(isect, pts, d: Vec, depth: float, axis: int) -> Access | None:
    clear = [(p, n) for p, n in pts
             if _dot(n, d) >= -NORMAL_TOL and math.isinf(_first_hit(isect, _axpy(p, n, RAY_OFFSET), d))]
    if not clear:
        return None
    gap = math.inf
    for p, n in clear:
        across = _axpy(n, d, -_dot(n, d))
        length = math.sqrt(_dot(across, across))
        if length > WALL_LIKE:
            unit = tuple(c / length for c in across)
            gap = min(gap, _first_hit(isect, _axpy(p, n, RAY_OFFSET), unit) + RAY_OFFSET)
    return Access(fraction=round(len(clear) / len(pts), 4), depth=round(depth, 4),
                  gap=None if math.isinf(gap) else round(gap, 4))


def _height_map(isect, lo: Vec, hi: Vec, d: str, top: float) -> list[tuple[float, float, float]]:
    """Top of material along d on a REACH_GRID grid over the stock footprint: (a, b, height along d)."""
    vec, axis = VECTORS[d], _AXIS_INDEX[d[1]]
    a, b = (i for i in range(3) if i != axis)
    out = []
    for i, j in itertools.product(range(math.ceil((hi[a] - lo[a]) / REACH_GRID)),
                                  range(math.ceil((hi[b] - lo[b]) / REACH_GRID))):
        p = [0.0, 0.0, 0.0]
        p[a], p[b] = lo[a] + (i + 0.5) * REACH_GRID, lo[b] + (j + 0.5) * REACH_GRID
        p[axis] = (top + 1.0) * vec[axis]
        t = _first_hit(isect, tuple(p), tuple(-c for c in vec))
        if not math.isinf(t):
            out.append((p[a], p[b], top + 1.0 - t))
    return out


def _thin_walls(boxes, d: str) -> list[tuple[float, float, float, float, float]]:
    """Faces narrower than two grid cells in plan view (wall sides, thin tops), as (a0, a1, b0, b1, top along d).
    The height map can miss them between rays; their bounding boxes can't."""
    vec, axis = VECTORS[d], _AXIS_INDEX[d[1]]
    a, b = (i for i in range(3) if i != axis)
    return [(lo[a], hi[a], lo[b], hi[b], max(_dot(lo, vec), _dot(hi, vec))) for lo, hi in boxes
            if min(hi[a] - lo[a], hi[b] - lo[b]) < 2 * REACH_GRID]


def _reach(hmap, flo: Vec, fhi: Vec, d: str, deepest: float, depth: float, walls=()) -> list[list[float]]:
    """Stick-out needed vs holder clearance (ADR 0008 Amendment 1). Each step starts where taller material
    begins, measured between bounding boxes, so a clearance can only be understated. A rise too small to
    start its own step joins the step before, which only asks for more stick-out sooner."""
    axis = _AXIS_INDEX[d[1]]
    a, b = (i for i in range(3) if i != axis)
    obstacles = [(x, x, y, y, h) for x, y, h in hmap] + list(walls)
    taller = sorted((math.hypot(max(flo[a] - a1, 0.0, a0 - fhi[a]), max(flo[b] - b1, 0.0, b0 - fhi[b])), h - deepest)
                    for a0, a1, b0, b1, h in obstacles if h - deepest > REACH_STEP)
    steps = [[0.0, 0.0]]
    for c, need in taller:
        if need <= steps[-1][1]:
            continue
        c = 0.0 if c < REACH_GRID else math.floor(c * 10) / 10
        if need > steps[-1][1] + REACH_STEP and c != steps[-1][0]:
            steps.append([c, round(need, 4)])
        else:
            steps[-1][1] = round(need, 4)
    return steps if len(steps) > 1 else [[0.0, round(depth, 4)]]


def _concave_cylinder(surf, pts) -> tuple[float, str | None] | None:
    """(radius, principal axis label) when the face is a cylinder whose normals point at its axis."""
    if surf.GetType() != GeomAbs_Cylinder or not pts:
        return None
    cyl = surf.Cylinder()
    axis = _xyz(cyl.Axis().Direction())
    p, n = pts[0]
    to_axis = _axpy(_xyz(cyl.Axis().Location()), p, -1.0)
    to_axis = _axpy(to_axis, axis, -_dot(to_axis, axis))
    if _dot(to_axis, n) <= 0:
        return None
    return cyl.Radius(), _principal(axis)


def _into_face(face, edge, t: float, n: Vec) -> Vec:
    """Direction from `edge` into `face`, tangent to it. OCCT keeps a face's interior to the left
    of its oriented edges, so it is n × tangent (checked against a point probe on 248 edges)."""
    exp = TopExp_Explorer(face, TopAbs_EDGE)
    while not exp.Current().IsSame(edge):
        exp.Next()
    oriented = TopoDS.Edge(exp.Current())
    p, v = gp_Pnt(), gp_Vec()
    BRepAdaptor_Curve(oriented).D1(t, p, v)
    s = -1.0 if oriented.Orientation() == TopAbs_REVERSED else 1.0
    tan = (s * v.X(), s * v.Y(), s * v.Z())
    return (n[1] * tan[2] - n[2] * tan[1], n[2] * tan[0] - n[0] * tan[2], n[0] * tan[1] - n[1] * tan[0])


def _edges(shape, faces: ShapeMap) -> list[Edge]:
    """Classify every edge between two distinct faces as tangent, concave or convex."""
    emap = EdgeFaceMap()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    out = set()
    for i in range(1, emap.Extent() + 1):
        pair = [TopoDS.Face(f) for f in emap.FindFromIndex(i)]
        if len(pair) != 2 or pair[0].IsSame(pair[1]):
            continue  # seam or free edge
        curve = BRepAdaptor_Curve(TopoDS.Edge(emap.FindKey(i)))
        mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
        normals = []
        for f in pair:
            u, v = GeomAPI_ProjectPointOnSurf(mid, BRep_Tool.Surface_s(f)).LowerDistanceParameters()
            pn = _normal(f, BRepAdaptor_Surface(f), u, v)
            normals.append(pn[1] if pn else None)
        if None in normals:
            continue
        a, b = sorted(faces.FindIndex(f) for f in pair)
        if _dot(normals[0], normals[1]) > TANGENT_DOT:
            out.add(Edge(a, b, "tangent"))
            continue
        # Concave when the second face's normal leans toward the first face's interior.
        # No point classification: the signal is sin(angle), so a 5° kink is as clear as a 90° corner.
        edge = TopoDS.Edge(emap.FindKey(i))
        t = 0.5 * (curve.FirstParameter() + curve.LastParameter())
        if _dot(normals[1], _into_face(pair[0], edge, t, normals[0])) <= 0:
            out.add(Edge(a, b, "convex"))
            continue
        axis = _principal(_xyz(curve.Line().Direction())) if curve.GetType() == GeomAbs_Line else None
        out.add(Edge(a, b, "concave", axis))
    return sorted(out, key=lambda e: (e.a, e.b, e.kind, e.axis or ""))


def _shared_line(a, b) -> tuple[Vec, Vec] | None:
    """Endpoints of the straight edge two faces share, if they share one."""
    exp = TopExp_Explorer(a, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge(exp.Current())
        inner = TopExp_Explorer(b, TopAbs_EDGE)
        while inner.More():
            if edge.IsSame(inner.Current()):
                curve = BRepAdaptor_Curve(edge)
                if curve.GetType() != GeomAbs_Line:
                    return None
                return _xyz(curve.Value(curve.FirstParameter())), _xyz(curve.Value(curve.LastParameter()))
            inner.Next()
        exp.Next()
    return None


def _edge_chamfers(fmap, faces: list[FaceFacts]) -> list[EdgeChamfer]:
    """RFC 0002 §1: planar, 45° ± 0.5° down and outward, bridging a stock side face and the stock bottom."""
    by_id = {f.id: f for f in faces}
    out = []
    for f in faces:
        if f.surface != "plane" or f.stock or f.normal is None:
            continue
        n = f.normal
        angle = math.degrees(math.acos(max(-1.0, min(1.0, -n[2]))))
        horizontal = (n[0], n[1], 0.0)
        h_len = math.hypot(n[0], n[1])
        if abs(angle - CHAMFER_ANGLE) > CHAMFER_TOL or h_len == 0:
            continue
        outward = next((d for d in ("+X", "-X", "+Y", "-Y")
                        if _dot(horizontal, VECTORS[d]) / h_len > PRINCIPAL_DOT), None)
        if outward is None:
            continue
        nbrs = [by_id[i] for i in f.stock_neighbors]
        side = next((g for g in nbrs if g.normal and _dot(g.normal, VECTORS[outward]) > PRINCIPAL_DOT), None)
        bottom = next((g for g in nbrs if g.normal and g.normal[2] < -PRINCIPAL_DOT), None)
        if side is None or bottom is None:
            continue
        face = TopoDS.Face(fmap.FindKey(f.id))
        line = _shared_line(face, TopoDS.Face(fmap.FindKey(bottom.id)))
        if line is None:
            continue
        direction = _axpy(line[1], line[0], -1.0)
        axis = _principal(direction)
        if axis not in ("X", "Y"):
            continue
        # Leg across the edge: the face's width perpendicular to the edge, within its plane, over √2.
        e = tuple(c / math.sqrt(_dot(direction, direction)) for c in direction)
        across = (n[1] * e[2] - n[2] * e[1], n[2] * e[0] - n[0] * e[2], n[0] * e[1] - n[1] * e[0])
        verts = TopExp_Explorer(face, TopAbs_VERTEX)
        points = []
        while verts.More():
            points.append(_xyz(BRep_Tool.Pnt_s(TopoDS.Vertex(verts.Current()))))
            verts.Next()
        width = max(_dot(p, across) for p in points) - min(_dot(p, across) for p in points)
        # Its length comes from the face itself: a chamfer can border several bottom faces (a slot splits them).
        along = [_dot(p, e) for p in points]
        start = _axpy(line[0], e, min(along) - _dot(line[0], e))
        end = _axpy(line[0], e, max(along) - _dot(line[0], e))
        out.append(EdgeChamfer(face_id=f.id, side_face=side.id, bottom_face=bottom.id, axis=axis, outward=outward,
                               angle=round(angle, 4), leg=round(width / math.sqrt(2), 4),
                               start=tuple(round(c, 6) for c in start), end=tuple(round(c, 6) for c in end)))
    return out


def extract(step_path: str | Path) -> PartFacts:
    """Read one solid from STEP and measure every face from the 6 principal directions."""
    t0 = time.perf_counter()
    path = Path(step_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    shape = _read(path)
    lo, hi = _bbox(shape)
    top = {d: max(_dot(lo, VECTORS[d]), _dot(hi, VECTORS[d])) for d in DIRECTIONS}  # stock top along d

    isect = IntCurvesFace_ShapeIntersector()
    isect.Load(shape, 1e-6)
    fmap = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    faces, hmaps, walls = [], {}, {}
    boxes = [_bbox(TopoDS.Face(fmap.FindKey(fid))) for fid in range(1, fmap.Extent() + 1)]
    for fid in range(1, fmap.Extent() + 1):
        face = TopoDS.Face(fmap.FindKey(fid))
        surf = BRepAdaptor_Surface(face)
        flo, fhi = boxes[fid - 1]
        pts = _samples(face, max(h - l for l, h in zip(flo, fhi)))
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        kind = _SURFACES.get(surf.GetType(), "other")
        normal = pts[0][1] if kind == "plane" and pts else None
        # Stock faces lie flat on the bounding box: the whole face, not just its lowest point.
        stock = normal is not None and any(
            _dot(normal, VECTORS[d]) > PRINCIPAL_DOT
            and fhi[_AXIS_INDEX[d[1]]] - flo[_AXIS_INDEX[d[1]]] < STOCK_TOL
            and abs(_dot(flo, VECTORS[d]) - top[d]) < STOCK_TOL
            for d in DIRECTIONS)
        access = {}
        for d in DIRECTIONS:
            vec, axis = VECTORS[d], _AXIS_INDEX[d[1]]
            deepest = flo[axis] if d[0] == "+" else -fhi[axis]
            a = _access(isect, pts, vec, top[d] - deepest, axis)
            if a:
                if d not in hmaps:
                    hmaps[d], walls[d] = _height_map(isect, lo, hi, d, top[d]), _thin_walls(boxes, d)
                a.reach = _reach(hmaps[d], flo, fhi, d, deepest, a.depth, walls[d])
                access[d] = a
        concave = _concave_cylinder(surf, pts)
        cyl = surf.Cylinder() if kind == "cylinder" else None
        faces.append(FaceFacts(
            id=fid, surface=kind, area=round(props.Mass(), 4),
            centroid=tuple(round(c, 4) for c in _xyz(props.CentreOfMass())),
            stock=stock, access=access, n_samples=len(pts),
            normal=tuple(round(c, 6) for c in normal) if normal else None,
            axis=tuple(round(c, 6) for c in _xyz(cyl.Axis().Direction())) if cyl else None,
            radius=round(cyl.Radius(), 4) if cyl else None,
            concave_radius=round(concave[0], 4) if concave else None,
            concave_axis=concave[1] if concave else None,
        ))
    edges = _edges(shape, fmap)
    stock = {f.id for f in faces if f.stock}
    for f in faces:
        f.stock_neighbors = sorted({e.b if e.a == f.id else e.a for e in edges
                                    if f.id in (e.a, e.b) and ({e.a, e.b} - {f.id}) <= stock})
    return PartFacts(source_sha256=digest, bbox=(lo, hi), faces=faces, edges=edges,
                     groups=face_groups(faces, edges), extract_s=round(time.perf_counter() - t0, 4),
                     edge_chamfers=_edge_chamfers(fmap, faces))
