"""A deterministic PNG of the part with one face group highlighted (RFC 0004).

Pure Python on OCP's own mesher: no display, no GPU, no imaging library. Orthographic z-buffer over
BRepMesh triangles, flat shading, B-rep edges drawn on top, 2× supersampled, written as PNG with zlib.
Face IDs are the ones `extract` reports (the same `_read` and `MapShapes` order).
"""

import math
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

from anville.geometry.step import EdgeFaceMap, ShapeMap, Vec, _bbox, _dot, _read, _xyz

# Views, camera side first. Isometric corners are preferred; the six axes are a fallback for a group no
# corner can see (a pocket floor seen only from straight above).
ISO_VIEWS: list[Vec] = [(1, -1, 1), (-1, -1, 1), (1, 1, 1), (-1, 1, 1),
                        (1, -1, -1), (-1, -1, -1), (1, 1, -1), (-1, 1, -1)]
AXIS_VIEWS: list[Vec] = [(0, 0, 1), (0, 0, -1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)]
SIZE = (800, 600)
SUPERSAMPLE = 2
PICK_SIZE = (160, 120)  # low-res pass that picks the view
MARGIN = 0.08           # of the shorter side
BACKGROUND = (246, 247, 249)
PART = (178, 186, 196)
HIGHLIGHT = (255, 106, 0)  # R > B, where the part, edges and background all have R < B: key on R - B > 0
EDGE = (44, 48, 56)
# Which overlay a flag needs, relative to the part on screen, not the canvas (a wider canvas doesn't draw a
# height-limited part any bigger). Measured on all 39 groups of Part A, Part B and the Onshape bracket, on the
# largest visible piece: every hole is at most 0.122 of the part's long side, everything else at least 0.236.
# Chamfer stripes fill 4-13% of their box and cover at most 1.8% of the part's box; the only other thin
# groups (big flange faces seen at an angle, 16%) cover 8%.
RING_MAX = 0.18   # of the part's long side: a piece smaller than this gets a ring
THIN_FILL = 0.2   # piece pixels / piece box area: below this, a long thin stripe gets an arrow ...
ARROW_MAX = 0.04  # ... if it also covers less than this share of the part's box


@dataclass
class Render:
    png: bytes
    width: int
    height: int
    view: Vec          # unit vector from the part toward the camera
    highlighted: int   # final-image pixels with any of the group showing (0: hidden from this view)
    box: tuple[int, int, int, int] | None  # x0, y0, x1, y1 (exclusive) around the pixels counted in
    # `highlighted`; None when none show. What shows, not the geometry: a through-hole's hidden bore would
    # stretch a geometric box to several times the rim the viewer sees.
    pieces: list[tuple[tuple[int, int, int, int], int]]  # each connected visible piece: (box, pixels), largest
    # first. Occlusion can split a group; `marker` reads the largest piece, a job may mark each.
    part_box: tuple[int, int, int, int]  # the whole part on screen: what the marker thresholds are relative to
    _right: Vec
    _up: Vec
    _centre: tuple[float, float]
    _scale: float

    @property
    def marker(self) -> str | None:
        """"ring" for a small group, "arrow" for a long thin one, None when the highlight speaks for itself
        or nothing shows."""
        if not self.pieces:
            return None
        (x0, y0, x1, y1), n = self.pieces[0]
        px0, py0, px1, py1 = self.part_box
        if max(x1 - x0, y1 - y0) < RING_MAX * max(px1 - px0, py1 - py0):
            return "ring"
        thin = n < THIN_FILL * (x1 - x0) * (y1 - y0)
        return "arrow" if thin and n < ARROW_MAX * (px1 - px0) * (py1 - py0) else None

    def project(self, p: Vec) -> tuple[float, float]:
        """Model point (mm) to image pixel (x right, y down), for captions and tests."""
        return (self.width / 2 + (_dot(p, self._right) - self._centre[0]) * self._scale,
                self.height / 2 - (_dot(p, self._up) - self._centre[1]) * self._scale)


def _unit(v) -> Vec:
    n = math.sqrt(_dot(v, v))
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _basis(view: Vec) -> tuple[Vec, Vec]:
    """Screen right and up for a camera on `view`: world Z stays up, or Y when looking along Z."""
    world_up = (0.0, 1.0, 0.0) if abs(view[2]) > 0.99 else (0.0, 0.0, 1.0)
    right = _unit(_cross(world_up, view))
    return right, _cross(view, right)


def _mesh(shape, fmap):
    """Triangles [(face_id, (p0, p1, p2), outward normal)], edges [(points, adjacent face ids)] (seams left
    out), and the IDs of faces the mesher gave no triangles."""
    lo, hi = _bbox(shape)
    size = max(h - l for l, h in zip(lo, hi))
    BRepMesh_IncrementalMesh(shape, size * 1e-3, False, 0.25, False)
    emap = EdgeFaceMap()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    tris, edges, seen, missing = [], [], ShapeMap(), []
    for fid in range(1, fmap.Extent() + 1):
        face = TopoDS.Face(fmap.FindKey(fid))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            missing.append(fid)
            continue
        trsf = loc.Transformation()
        nodes = [_xyz(tri.Node(i).Transformed(trsf)) for i in range(1, tri.NbNodes() + 1)]
        flip = face.Orientation() == TopAbs_REVERSED
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            if flip:
                b, c = c, b
            p = (nodes[a - 1], nodes[b - 1], nodes[c - 1])
            n = _cross(tuple(q - r for q, r in zip(p[1], p[0])), tuple(q - r for q, r in zip(p[2], p[0])))
            if _dot(n, n) > 1e-24:
                tris.append((fid, p, _unit(n)))
        ex = TopExp_Explorer(face, TopAbs_EDGE)
        while ex.More():
            edge = TopoDS.Edge(ex.Current())
            ex.Next()
            if seen.Contains(edge) or BRep_Tool.IsClosed_s(edge, face):
                continue
            seen.Add(edge)
            poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
            if poly is not None:
                adjacent = {fmap.FindIndex(f) for f in emap.FindFromKey(edge)}
                edges.append(([nodes[poly.Node(i) - 1] for i in range(1, poly.NbNodes() + 1)], adjacent))
    return tris, edges, missing


def _camera(tris, view: Vec, w: int, h: int):
    right, up = _basis(view)
    xs = [_dot(p, right) for _, ps, _ in tris for p in ps]
    ys = [_dot(p, up) for _, ps, _ in tris for p in ps]
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    m = MARGIN * min(w, h)
    scale = min((w - 2 * m) / max(max(xs) - min(xs), 1e-9), (h - 2 * m) / max(max(ys) - min(ys), 1e-9))
    return right, up, centre, scale


def _raster(tris, view, right, up, centre, scale, w, h):
    """Z-buffer: the front triangle's index per pixel (-1 for background), and the depth buffer."""
    zbuf = [-math.inf] * (w * h)
    ibuf = [-1] * (w * h)
    for t, (_, ps, n) in enumerate(tris):
        if _dot(n, view) <= 0:  # back face of a closed solid: always hidden
            continue
        s = [(w / 2 + (_dot(p, right) - centre[0]) * scale, h / 2 - (_dot(p, up) - centre[1]) * scale,
              _dot(p, view)) for p in ps]
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = s
        det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(det) < 1e-12:
            continue
        a = ((z1 - z0) * (y2 - y0) - (z2 - z0) * (y1 - y0)) / det  # dz/dx
        b = ((x1 - x0) * (z2 - z0) - (x2 - x0) * (z1 - z0)) / det  # dz/dy
        sign = 1.0 if det > 0 else -1.0
        es = [(xa, ya, xb, yb) for (xa, ya, _), (xb, yb, _) in ((s[0], s[1]), (s[1], s[2]), (s[2], s[0]))]
        for py in range(max(0, math.ceil(min(y0, y1, y2) - 0.5)), min(h - 1, math.floor(max(y0, y1, y2) - 0.5)) + 1):
            yc = py + 0.5
            lo, hi = -math.inf, math.inf
            for xa, ya, xb, yb in es:
                # inside when sign * ((xb - xa) * (yc - ya) - (yb - ya) * (x - xa)) >= 0
                k = -sign * (yb - ya)
                m = sign * ((xb - xa) * (yc - ya) + (yb - ya) * xa)
                if abs(k) < 1e-12:
                    if m < 0:  # a horizontal edge with this row outside it
                        lo, hi = 1.0, 0.0
                        break
                elif k > 0:
                    lo = max(lo, -m / k)
                else:
                    hi = min(hi, -m / k)
            if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
                continue
            xa_, xb_ = max(0, math.ceil(lo - 0.5)), min(w - 1, math.floor(hi - 0.5))
            if xa_ > xb_:
                continue
            z = z0 + a * (xa_ + 0.5 - x0) + b * (yc - y0)
            row = py * w
            for i in range(row + xa_, row + xb_ + 1):
                if z > zbuf[i]:
                    zbuf[i], ibuf[i] = z, t
                z += a
    return ibuf, zbuf


def _visible(tris, group: set[int], view: Vec, w: int, h: int) -> int:
    right, up, centre, scale = _camera(tris, view, w, h)
    ibuf, _ = _raster(tris, view, right, up, centre, scale, w, h)
    return sum(1 for t in ibuf if t >= 0 and tris[t][0] in group)


def _pick(tris, group: set[int]) -> Vec:
    """The isometric corner that shows the most of the group; an axis view only if no corner shows any."""
    for views in (ISO_VIEWS, AXIS_VIEWS):
        counts = [_visible(tris, group, _unit(v), *PICK_SIZE) for v in views]
        if max(counts) > 0:
            return _unit(views[counts.index(max(counts))])
    return _unit(ISO_VIEWS[0])


def _png(w: int, h: int, rgb: bytearray) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def render(step_path: str | Path, faces: Iterable[int], view: Vec | None = None,
           size: tuple[int, int] = SIZE) -> Render:
    """PNG of the part with `faces` (extract's face IDs) highlighted. With no `view`, the camera goes to
    the isometric corner that shows the most of them."""
    shape = _read(Path(step_path))
    fmap = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    group = set(faces)
    bad = sorted(f for f in group if not 1 <= f <= fmap.Extent())
    if bad:
        raise ValueError(f"no face {bad} on this part (it has F1..F{fmap.Extent()})")
    tris, edges, missing = _mesh(shape, fmap)
    if not tris:
        raise ValueError("the mesher produced no triangles for this part: no render")
    unmeshed = sorted(group.intersection(missing))
    if unmeshed:
        raise ValueError(f"no mesh for {', '.join(f'F{f}' for f in unmeshed)}: can't show it")
    view = _unit(view) if view else _pick(tris, group)

    w, h = size
    k = SUPERSAMPLE
    W, H = w * k, h * k
    right, up, centre, scale = _camera(tris, view, W, H)
    ibuf, zbuf = _raster(tris, view, right, up, centre, scale, W, H)

    light = _unit(tuple(v + 0.5 * u + 0.3 * r for v, u, r in zip(view, up, right)))
    shade = [0.45 + 0.55 * max(0.0, _dot(n, light)) for _, _, n in tris]
    big = bytearray(BACKGROUND * (W * H))
    lit = [False] * (W * H)
    for i, t in enumerate(ibuf):
        if t >= 0:
            hl = tris[t][0] in group
            base, s = (HIGHLIGHT if hl else PART), shade[t]
            big[3 * i:3 * i + 3] = bytes(int(c * s) for c in base)
            lit[i] = hl

    # An edge sample draws only over one of its own faces (or the background, at the silhouette) and within
    # two supersampled pixels of that face's depth. Depth alone let edges under a thin skin show through it.
    eps = 2.0 / scale
    for pts, adjacent in edges:
        for p, q in zip(pts, pts[1:]):
            sp = [(W / 2 + (_dot(r, right) - centre[0]) * scale, H / 2 - (_dot(r, up) - centre[1]) * scale,
                   _dot(r, view)) for r in (p, q)]
            n = max(1, math.ceil(2 * max(abs(sp[1][0] - sp[0][0]), abs(sp[1][1] - sp[0][1]))))
            for j in range(n + 1):
                f = j / n
                x, y, z = (sp[0][c] + f * (sp[1][c] - sp[0][c]) for c in range(3))
                for dx in range(k):
                    for dy in range(k):
                        px, py = int(x - k / 2 + dx + 0.5), int(y - k / 2 + dy + 0.5)
                        if not (0 <= px < W and 0 <= py < H):
                            continue
                        i = py * W + px
                        if z >= zbuf[i] - eps and (ibuf[i] < 0 or tris[ibuf[i]][0] in adjacent):
                            big[3 * i:3 * i + 3] = bytes(EDGE)
                            lit[i] = False

    rgb, highlighted, xs, ys = bytearray(w * h * 3), 0, [], []
    for y in range(h):
        for x in range(w):
            acc, hl = [0, 0, 0], 0
            for dy in range(k):
                for dx in range(k):
                    i = (y * k + dy) * W + x * k + dx
                    acc[0] += big[3 * i]
                    acc[1] += big[3 * i + 1]
                    acc[2] += big[3 * i + 2]
                    hl += lit[i]
            o = 3 * (y * w + x)
            rgb[o:o + 3] = bytes(c // (k * k) for c in acc)
            if hl:  # any orange left in the pixel shows
                highlighted += 1
                xs.append(x)
                ys.append(y)
    box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if xs else None
    out = Render(_png(w, h, rgb), w, h, view, highlighted, box, _pieces(xs, ys), (0, 0, w, h),
                 right, up, centre, scale / k)
    corners = [out.project(q) for _, ps, _ in tris for q in ps]
    out.part_box = (math.floor(min(x for x, _ in corners)), math.floor(min(y for _, y in corners)),
                    math.ceil(max(x for x, _ in corners)), math.ceil(max(y for _, y in corners)))
    return out


def _pieces(xs: list[int], ys: list[int]) -> list[tuple[tuple[int, int, int, int], int]]:
    """8-connected pieces of the lit pixels: (box, pixels), largest first (ties: top-left first)."""
    left = set(zip(xs, ys))
    out = []
    while left:
        start = min(left, key=lambda q: (q[1], q[0]))
        left.discard(start)
        stack, piece = [start], []
        while stack:
            x, y = stack.pop()
            piece.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in left:
                        left.discard(q)
                        stack.append(q)
        bx = [x for x, _ in piece]
        by = [y for _, y in piece]
        out.append(((min(bx), min(by), max(bx) + 1, max(by) + 1), len(piece)))
    return sorted(out, key=lambda b: (-b[1], b[0][1], b[0][0]))
