"""render(): a PNG of Part A with a face group highlighted (RFC 0004).

The check runs by a second route: G3's B-rep vertices, projected through the render's camera, must
bound the orange pixels decoded from the PNG, and those pixels must fill most of that polygon.
"""

import math
import struct
import zlib

import pytest
from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS

from anville.geometry import extract
from anville.geometry.render import render
from anville.geometry.step import ShapeMap, _read, _xyz
from tests.geometry.parts import demo_bracket, demo_bracket_b, write_step


@pytest.fixture(scope="module")
def part_a(tmp_path_factory):
    return write_step(demo_bracket(), tmp_path_factory.mktemp("render") / "part_a.step")


def _pixels(png: bytes):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert (depth, colour) == (8, 2)
    i, data = 8, b""
    while i < len(png):
        n, kind = struct.unpack(">I4s", png[i:i + 8])
        if kind == b"IDAT":
            data += png[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(data)
    rows = [raw[y * (3 * w + 1):(y + 1) * (3 * w + 1)] for y in range(h)]
    assert all(r[0] == 0 for r in rows)
    return w, h, [[tuple(r[1 + 3 * x:4 + 3 * x]) for x in range(w)] for r in rows]


def _orange(p):
    r, g, b = p
    return b < 40 and r > 60 and r > 1.6 * g


def _vertices(step, fid):
    fmap = ShapeMap()
    TopExp.MapShapes_s(_read(step), TopAbs_FACE, fmap)
    ex, pts = TopExp_Explorer(fmap.FindKey(fid), TopAbs_VERTEX), set()
    while ex.More():
        pts.add(tuple(round(c, 6) for c in _xyz(BRep_Tool.Pnt_s(TopoDS.Vertex(ex.Current())))))
        ex.Next()
    return sorted(pts)


def _hull(pts):
    pts = sorted(set(pts))
    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out
    return half(pts)[:-1] + half(pts[::-1])[:-1]


def _inside(poly, x, y, pad):
    """Inside a convex counter-clockwise polygon, or within `pad` pixels of it."""
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if cross < -pad * ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5:
            return False
    return True


def _area(poly):
    return abs(sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]))) / 2


def test_g3_lights_up_where_it_projects(part_a):
    facts = extract(part_a)
    g3 = facts.group_faces("G3")
    assert g3 == [3] and facts.faces[2].normal[2] < -0.5  # the bottom weld-clearance chamfer
    r = render(part_a, g3)
    assert r.view[2] < 0  # it faces down, so the camera went below
    w, h, px = _pixels(r.png)
    assert (w, h) == (r.width, r.height)
    lit = [(x + 0.5, y + 0.5) for y in range(h) for x in range(w) if _orange(px[y][x])]
    poly = _hull([r.project(p) for p in _vertices(part_a, 3)])
    inside = sum(_inside(poly, x, y, pad=1.5) for x, y in lit)
    assert len(lit) > 0.7 * _area(poly)   # most of the face shows
    assert inside == len(lit)             # and nothing orange sits off it
    assert r.highlighted == sum(_tinted(p) for row in px for p in row)


def test_same_input_same_bytes(part_a):
    assert render(part_a, [3]).png == render(part_a, [3]).png


def test_a_hidden_group_reports_zero(part_a):
    """The top face, seen from below: nothing to show, and the render says so."""
    r = render(part_a, extract(part_a).group_faces("G4"), view=(1, -1, -1))
    w, h, px = _pixels(r.png)
    assert r.highlighted == 0 and not any(_orange(p) for row in px for p in row)


def test_unknown_faces_are_refused(part_a):
    with pytest.raises(ValueError, match="no face"):
        render(part_a, [3, 99])


# Codex review of the spike: each of these failed before its fix.

def test_edges_under_a_thin_skin_stay_hidden(tmp_path):
    """A pocket cut from below leaves a 0.02 mm skin: the pocket's rim edges sit under it, inside the old
    depth tolerance, and must not draw through it."""
    from tests.geometry.parts import _box, _cut
    step = write_step(_cut(_box(0, 0, 0, 40, 40, 10), _box(10, 10, 0, 20, 20, 9.98)), tmp_path / "skin.step")
    top = next(f.id for f in extract(step).faces if f.normal and f.normal[2] > 0.99)
    r = render(step, [top], view=(0, 0, 1))
    _, _, px = _pixels(r.png)
    lo, hi = r.project((7, 7, 10)), r.project((33, 33, 10))  # the rim at 10 and 30, not the part's edges
    xs, ys = sorted((lo[0], hi[0])), sorted((lo[1], hi[1]))
    assert all(_orange(px[y][x]) and px[y][x][0] > 200  # solid orange: no edge stroke, not even half a pixel
               for y in range(int(ys[0]), int(ys[1])) for x in range(int(xs[0]), int(xs[1])))


def _tinted(p):
    return p[0] > p[2]  # any orange in the mix: grey, edges and background all have R < B


def test_highlighted_counts_the_orange_that_shows(part_a):
    """Shrinking Part A thins the chamfer to under a pixel, then edge strokes cover it. `highlighted` follows
    what's on screen: any orange counts, and orange painted over by an edge doesn't."""
    for size in ((40, 30), (24, 18), (12, 9)):
        r = render(part_a, [3], size=size)
        _, _, px = _pixels(r.png)
        tinted = sum(_tinted(p) for row in px for p in row)
        assert (r.highlighted > 0) == (tinted > 0), (size, r.highlighted, tinted)
        assert r.highlighted == tinted, (size, r.highlighted, tinted)


def test_holes_in_a_highlighted_face_stay_unlit(part_a):
    """The bottom face (G5) seen from below: the two mount holes go through it and must not be orange."""
    r = render(part_a, extract(part_a).group_faces("G5"), view=(0, 0, -1))
    _, _, px = _pixels(r.png)
    for centre in ((12, 30, 0), (88, 30, 0)):
        x, y = r.project(centre)
        assert not _orange(px[int(y)][int(x)])
    x, y = r.project((50, 5, 0))
    assert _orange(px[int(y)][int(x)])


def test_an_unmeshed_face_is_refused_not_dropped(part_a, monkeypatch):
    import anville.geometry.render as R
    from anville.geometry.step import ShapeMap as _SM
    fmap = _SM()
    TopExp.MapShapes_s(_read(part_a), TopAbs_FACE, fmap)
    real = R.BRep_Tool

    class NoMesh:
        IsClosed_s = staticmethod(real.IsClosed_s)
        PolygonOnTriangulation_s = staticmethod(real.PolygonOnTriangulation_s)

        @staticmethod
        def Triangulation_s(face, loc):
            return None if face.IsSame(target[0]) else real.Triangulation_s(face, loc)

    target = []
    monkeypatch.setattr(R, "BRep_Tool", NoMesh)
    monkeypatch.setattr(R, "_read", lambda p: (s := _read(p), target.append(_face(s, 3)))[0])
    with pytest.raises(ValueError, match="F3"):
        render(part_a, [3])
    target.clear()
    monkeypatch.setattr(R, "_read", lambda p: (s := _read(p), target.append(s))[0])
    NoMesh.Triangulation_s = staticmethod(lambda face, loc: None)
    with pytest.raises(ValueError, match="mesh"):
        render(part_a, [3])


def _face(shape, fid):
    fmap = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    return fmap.FindKey(fid)


def test_box_bounds_what_shows(part_a):
    """`box` is what tells a ring from an arrow: the extent of the orange on screen, checked against G3's
    corners projected by the second route. A hole's box is its rim, not its 20 mm bore."""
    r = render(part_a, [3])
    _, _, px = _pixels(r.png)
    x0, y0, x1, y1 = r.box
    lit = [(x, y) for y, row in enumerate(px) for x, p in enumerate(row) if _tinted(p)]
    assert (x0, y0, x1, y1) == (min(x for x, _ in lit), min(y for _, y in lit),
                                max(x for x, _ in lit) + 1, max(y for _, y in lit) + 1)
    hull = [r.project(p) for p in _vertices(part_a, 3)]  # a planar chamfer: its corners bound it
    for got, want in ((x0, min(x for x, _ in hull)), (x1, max(x for x, _ in hull)),
                      (y0, min(y for _, y in hull)), (y1, max(y for _, y in hull))):
        assert abs(got - want) < 1.5
    assert max(x1 - x0, y1 - y0) > 300  # long and thin: the arrow case

    x0, y0, x1, y1 = render(part_a, extract(part_a).group_faces("G9")).box
    assert 10 < max(x1 - x0, y1 - y0) < 60  # a Ø8.5 hole: the ring case

    assert render(part_a, extract(part_a).group_faces("G4"), view=(1, -1, -1)).box is None


@pytest.mark.parametrize("group, marker", [("G9", "ring"), ("G3", "arrow"), ("G6", "arrow"), ("G10", None),
                                           ("G4", None)])
def test_marker_rings_holes_and_points_at_stripes(part_a, group, marker):
    """Holes get a ring, chamfer stripes an arrow, the pocket walls and big faces neither."""
    assert render(part_a, extract(part_a).group_faces(group)).marker == marker


def test_no_marker_when_nothing_shows(part_a):
    assert render(part_a, extract(part_a).group_faces("G4"), view=(1, -1, -1)).marker is None


def test_a_big_face_at_an_angle_gets_no_arrow():
    """The bracket's flange face (G9) fills only 16% of its box when seen from a corner, but it is 5% of the
    image: plainly visible, so no arrow."""
    step = "tests/geometry/data/onshape_z_bracket.step"
    r = render(step, extract(step).group_faces("G9"))
    assert r.highlighted > 0.04 * r.width * r.height and r.marker is None


# Codex, second pass: each failed before its fix.

def _fake(pieces, part_box=(48, 48, 752, 552)):
    """A Render with only the fields `marker` reads: pieces [(box, pixels)], largest first."""
    from anville.geometry.render import Render
    return Render(png=b"", width=800, height=600, view=(0, 0, 1), highlighted=sum(n for _, n in pieces),
                  box=None, pieces=pieces, part_box=part_box,
                  _right=(1, 0, 0), _up=(0, 1, 0), _centre=(0, 0), _scale=1)


def test_two_far_pieces_are_two_rings_not_an_arrow():
    """Two 20×20 patches 560 px apart: the gap made the combined box look like a thin stripe."""
    r = _fake([((100, 200, 120, 220), 400), ((680, 200, 700, 220), 400)])
    assert r.marker == "ring"


def test_the_marker_thresholds_are_pinned():
    """One pixel either side of each cutoff, against a 704×504 part box."""
    from anville.geometry.render import ARROW_MAX, RING_MAX, THIN_FILL
    edge = RING_MAX * 704
    assert _fake([((0, 0, math.ceil(edge) - 1, 10), 10 * (math.ceil(edge) - 1))]).marker == "ring"
    assert _fake([((0, 0, math.floor(edge) + 1, 10), 10 * (math.floor(edge) + 1))]).marker is None

    part = 704 * 504
    side = int(math.sqrt(0.5 * ARROW_MAX * part / THIN_FILL))  # a box whose thin fill is under ARROW_MAX
    box, area = (0, 0, side, side), side * side
    assert _fake([(box, math.ceil(THIN_FILL * area) - 1)]).marker == "arrow"
    assert _fake([(box, math.ceil(THIN_FILL * area) + 1)]).marker is None
    big = (0, 0, 700, 500)
    assert _fake([(big, math.floor(ARROW_MAX * part) - 1)]).marker == "arrow"
    assert _fake([(big, math.floor(ARROW_MAX * part) + 1)]).marker is None  # thin, but too much of the part


@pytest.mark.parametrize("group", ["G9", "G3", "G10"])
def test_the_marker_survives_a_wider_canvas(part_a, group):
    """Part A at 1600×600 is height-limited, so it is drawn no bigger than at 800×600: same marker."""
    ids = extract(part_a).group_faces(group)
    assert render(part_a, ids, size=(1600, 600)).marker == render(part_a, ids).marker


def test_the_bore_stays_unmarked_on_a_wider_canvas():
    """The bracket's Ø44.5 bore (G12) is the group nearest the ring cutoff: 174 px at 800×600."""
    step = "tests/geometry/data/onshape_z_bracket.step"
    ids = extract(step).group_faces("G12")
    assert render(step, ids).marker is None
    assert render(step, ids, size=(1600, 600)).marker is None


@pytest.mark.parametrize("part, group, marker", [
    ("B", "G8", "ring"),     # the largest hole: 0.122 of the part's long side
    ("B", "G3", "arrow"),    # the chamfer that fills the most of its box: 13%
    ("bracket", "G12", None),  # the smallest non-hole: 0.247
    ("bracket", "G9", None),   # a big face at an angle: thin (16%) but 8% of the part
    ("bracket", "G11", "ring"),
])
def test_the_groups_nearest_each_cutoff(part, group, marker, tmp_path):
    step = ("tests/geometry/data/onshape_z_bracket.step" if part == "bracket"
            else write_step(demo_bracket_b(), tmp_path / "b.step"))
    assert render(step, extract(step).group_faces(group)).marker == marker


def test_the_cutoffs_are_the_rfcs():
    from anville.geometry.render import ARROW_MAX, RING_MAX, THIN_FILL
    assert (RING_MAX, THIN_FILL, ARROW_MAX) == (0.18, 0.2, 0.04)  # docs/rfcs/0004-part-render.md
