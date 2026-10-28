"""Synthetic prismatic aluminum test parts, built in code (locked decision 7).

Dimensions in mm. Every part is a block whose bounding box is the stock (ADR 0008 §2).
Run `python -m tests.geometry.parts <dir>` to write them all as STEP files.
"""

import math
from pathlib import Path

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.GeomAbs import GeomAbs_Line
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS


def _box(x0, y0, z0, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _pocket_body(x0, y0, z0, dx, dy, dz, r):
    """A box with its four Z-parallel edges filleted at r: cutting it leaves a pocket with r corners."""
    body = _box(x0, y0, z0, dx, dy, dz)
    fillet = BRepFilletAPI_MakeFillet(body)
    exp = TopExp_Explorer(body, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge(exp.Current())
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() == GeomAbs_Line and abs(curve.Line().Direction().Z()) > 0.99:
            fillet.Add(r, edge)
        exp.Next()
    return fillet.Shape()


def _cylinder(base, direction, r, h):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, h).Shape()


def _cut(a, b):
    return BRepAlgoAPI_Cut(a, b).Shape()


def _fuse(a, b):
    return BRepAlgoAPI_Fuse(a, b).Shape()


def plain_block():
    """80×50×25. Expect 1 setup: nothing to machine beyond squared stock."""
    return _box(0, 0, 0, 80, 50, 25)


def two_pockets():
    """Top pocket 40×25×8 and bottom pocket 30×20×6, both r5 corners. Expect 2 setups (+Z, −Z), EM6."""
    part = _cut(plain_block(), _pocket_body(20, 12.5, 17, 40, 25, 10, 5))
    return _cut(part, _pocket_body(25, 15, -2, 30, 20, 8, 5))


def side_hole():
    """two_pockets plus an Ø8 blind hole, 15 deep, from the +X face. Expect 3 setups (+Z, −Z, +X)."""
    return _cut(two_pockets(), _cylinder((90, 25, 12.5), (-1, 0, 0), 4, 25))


def deep_slot():
    """80×50×60 with a 3 mm wide, 45 deep round-ended slot. No tool reaches: slot faces unreachable."""
    slot = _fuse(_box(20, 23.5, 15, 40, 3, 50), _cylinder((20, 25, 15), (0, 0, 1), 1.5, 50))
    slot = _fuse(slot, _cylinder((60, 25, 15), (0, 0, 1), 1.5, 50))
    return _cut(_box(0, 0, 0, 80, 50, 60), slot)


def tight_corner():
    """Top pocket 40×25×10 with r2 corners. Expect 1 setup; the corners force EM3 (≤ Ø4)."""
    return _cut(plain_block(), _pocket_body(20, 12.5, 15, 40, 25, 15, 2))


def clevis():
    """60×30×30 with an open 10 mm slot through X, flat bottom, no radius anywhere. Expect 1 setup, EM6."""
    return _cut(_box(0, 0, 0, 60, 30, 30), _box(-5, 10, 10, 70, 10, 25))


def undercut():
    """Top pocket with an Ø6 bore drilled sideways into its +X wall. Expect the bore unreachable: no direction."""
    part = _cut(plain_block(), _pocket_body(20, 12.5, 10, 40, 25, 20, 5))
    return _cut(part, _cylinder((59, 25, 16), (1, 0, 0), 3, 10))


def _prism(points, vector):
    """Extrude a closed polygon (list of xyz points) along `vector`."""
    poly = BRepBuilderAPI_MakePolygon()
    for p in points:
        poly.Add(gp_Pnt(*p))
    poly.Close()
    return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(), gp_Vec(*vector)).Shape()


def wedge():
    """80×50×25 whose bottom rises 1.6 mm along X (about 1.1°). Expect 1 setup from −Z: the slope is not stock."""
    return _prism([(0, 0, 0), (80, 0, 1.6), (80, 0, 25), (0, 0, 25)], (0, 50, 0))


def kinked_pocket():
    """Top pocket, r5 corners, whose +Y wall bends 5° at a sharp edge parallel to Z (x=40). Expect that edge flagged."""
    apex_y = 37.5 + 20 * math.tan(math.radians(2.5))
    body = _prism([(20, 12.5, 15), (60, 12.5, 15), (60, 37.5, 15), (40, apex_y, 15), (20, 37.5, 15)], (0, 0, 15))
    fillet = BRepFilletAPI_MakeFillet(body)
    exp = TopExp_Explorer(body, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge(exp.Current())
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() == GeomAbs_Line and abs(curve.Line().Direction().Z()) > 0.99 \
                and abs(curve.Value(curve.FirstParameter()).X() - 40) > 1:  # keep the kink sharp
            fillet.Add(5, edge)
        exp.Next()
    return _cut(plain_block(), fillet.Shape())


PARTS = {f.__name__: f for f in (plain_block, two_pockets, side_hole, deep_slot, tight_corner, clevis, undercut,
                                 wedge, kinked_pocket)}


def write_step(shape, path: Path) -> Path:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise OSError(f"STEP write failed: {path}")
    return path
