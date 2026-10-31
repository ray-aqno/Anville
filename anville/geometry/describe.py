"""describe_group: a face group as a noun phrase in an engineer's words, measured by code ("the 1.5 mm chamfer
on the bottom front edge"), so a comment, a render label or a setup sheet needn't say "G11".

Positions are relative to the part's own bounding box, never the world origin: a part modelled centred
on the origin reads the same as one modelled at a corner. Sides follow Onshape's named views: -Y is the
front, +Z the top. A group that fits no rule exactly gets None, and the caller says "G11": a
description is never guessed.
"""

from __future__ import annotations

import math

from anville.geometry.facts import FaceFacts, PartFacts

TOL = 0.01          # mm: extents that must agree (a through hole's length, a chamfer's two legs)
ANGLE_TOL = 0.5     # degrees
SIDES = {"+Z": "top", "-Z": "bottom", "-Y": "front", "+Y": "back", "-X": "left", "+X": "right"}
_AXIS = {"X": 0, "Y": 1, "Z": 2}


def describe_group(facts: PartFacts, group: str) -> str | None:
    """A noun phrase that drops into a sentence ("the left Ø8.5 through hole"), or None when no rule fits
    the group exactly. Raises ValueError for a label the part doesn't have."""
    by_id = {f.id: f for f in facts.faces}
    faces = [by_id[i] for i in facts.group_faces(group)]
    for rule in (_stock, _chamfer, _hole, _floor, _walls):
        text = rule(facts, faces, by_id)
        if text:
            return text
    return None


def _n(x: float) -> str:
    """A measurement as an engineer writes it: 8, 8.5, 1.25."""
    return f"{round(x, 2):g}"


def _principal(v) -> str | None:
    """"+Z" etc. when v is within ANGLE_TOL of a principal axis."""
    if not v:
        return None
    i = max(range(3), key=lambda k: abs(v[k]))
    if abs(v[i]) < math.cos(math.radians(ANGLE_TOL)):
        return None
    return ("+" if v[i] > 0 else "-") + "XYZ"[i]


def _extent(box, i: int) -> float:
    return box[1][i] - box[0][i]


def _union(faces: list[FaceFacts]):
    if any(not f.box for f in faces):
        return None
    return ([min(f.box[0][i] for f in faces) for i in range(3)], [max(f.box[1][i] for f in faces) for i in range(3)])


def _where(facts: PartFacts, point, skip: str) -> str:
    """"front left", "right", "" (centred), from thirds of the part's bbox in X and Y (Z for a Z-skipped hole
    is the axis itself). `skip` is the axis to leave out."""
    lo, hi = facts.bbox
    words = []
    for axis, low, high in (("Y", "front", "back"), ("X", "left", "right")):
        if axis == skip:
            continue
        i = _AXIS[axis]
        t = (point[i] - lo[i]) / max(hi[i] - lo[i], 1e-9)
        words.append(low if t < 1 / 3 else high if t > 2 / 3 else "")
    return " ".join(w for w in words if w)


def _stock(facts, faces, by_id) -> str | None:
    if len(faces) == 1 and faces[0].stock:
        d = _principal(faces[0].normal)
        if d:
            return f"the {SIDES[d]} face"
    return None


def _chamfer(facts, faces, by_id) -> str | None:
    """One plane at 45° between ±Z and a horizontal axis, running along an outer edge of the part."""
    if len(faces) != 1 or faces[0].surface != "plane" or faces[0].stock or not faces[0].normal or not faces[0].box:
        return None
    f, (lo, hi) = faces[0], facts.bbox
    nx, ny, nz = f.normal
    if abs(math.degrees(math.acos(min(1.0, abs(nz)))) - 45.0) > ANGLE_TOL:
        return None
    m = math.hypot(nx, ny)
    side = _principal((nx / m, ny / m, 0.0)) if m > 1e-9 else None  # the horizontal side it faces
    if side not in ("+X", "-X", "+Y", "-Y"):
        return None
    across, along = _AXIS[side[1]], 1 - _AXIS[side[1]]  # Y-facing runs along X, and the reverse
    leg_h, leg_v = _extent(f.box, across), _extent(f.box, 2)
    if abs(leg_h - leg_v) > TOL:
        return None
    vertical = "top" if nz > 0 else "bottom"
    on_edge = (abs(f.box[1][2] - hi[2]) <= TOL if nz > 0 else abs(f.box[0][2] - lo[2]) <= TOL) and \
        (abs(f.box[1][across] - hi[across]) <= TOL if side[0] == "+" else abs(f.box[0][across] - lo[across]) <= TOL)
    if not on_edge:
        return None
    return f"the {_n(leg_h)} mm chamfer on the {vertical} {SIDES[side]} edge"


def _inside(facts: PartFacts, box, skip: int) -> bool:
    """The box lies strictly inside the part's bbox on every axis but `skip`: enclosed, not a step or an
    open slot that runs out through a side."""
    lo, hi = facts.bbox
    return all(box[0][k] > lo[k] + TOL and box[1][k] < hi[k] - TOL for k in range(3) if k != skip)


def _hole(facts, faces, by_id) -> str | None:
    """Concave cylinders of one radius on one principal axis: through when they run the part's full length
    along it, blind when they start at one side of the part and only that direction reaches them."""
    if any(f.surface != "cylinder" or not f.concave_radius or not f.concave_axis for f in faces):
        return None
    radii, axes = {round(f.concave_radius, 3) for f in faces}, {f.concave_axis for f in faces}
    box = _union(faces)
    if len(radii) != 1 or len(axes) != 1 or box is None:
        return None
    (axis,), i = axes, _AXIS[next(iter(axes))]
    lo, hi = facts.bbox
    r = radii.pop()
    if not _full_circle(faces, r, _extent(box, i)):
        return None  # a half-round channel or a partial bore, not a hole (Codex, describe review)
    size = f"Ø{_n(2 * r)}"
    at_lo, at_hi = abs(box[0][i] - lo[i]) <= TOL, abs(box[1][i] - hi[i]) <= TOL
    if at_lo and at_hi:
        where = _where(facts, [(a + b) / 2 for a, b in zip(*box)], axis)
        return f"the {where + ' ' if where else ''}{size} through hole"
    d = f"+{axis}" if at_hi else f"-{axis}" if at_lo else None
    if d is None or not all(d in f.access and _flip(d) not in f.access for f in faces):
        return None  # an internal bore, or one reached both ways: not described yet
    if not any(_is_disc(by_id[n], r, axis) for n in _neighbours(facts, {f.id for f in faces}, "concave")):
        return None  # no flat bottom of the hole's own size: a counterbore, or a drilled point
    return f"the {_n(_extent(box, i))} mm deep {size} hole in the {SIDES[d]} face"


def _full_circle(faces, r: float, length: float) -> bool:
    return abs(sum(f.area for f in faces) - 2 * math.pi * r * length) <= 0.01 * 2 * math.pi * r * length


def _is_disc(f: FaceFacts, r: float, axis: str) -> bool:
    """A flat face across `axis` with the area of a circle of radius r: the bottom of a hole, not a shoulder."""
    return f.surface == "plane" and _principal(f.normal) in (f"+{axis}", f"-{axis}") \
        and abs(f.area - math.pi * r * r) <= 0.01 * math.pi * r * r


def _neighbours(facts: PartFacts, ids: set[int], kind: str) -> set[int]:
    """Faces outside `ids` that share a `kind` edge with one inside."""
    return {e.b if e.a in ids else e.a for e in facts.edges if e.kind == kind and (e.a in ids) != (e.b in ids)}


def _flip(d: str) -> str:
    return ("-" if d[0] == "+" else "+") + d[1]


def _corner_radius(faces, facts, by_id, normal_axis: str) -> float | None:
    """The one radius of the concave fillets on the outline of these faces, along `normal_axis`; None if none or
    several. A hole inside the outline isn't a corner."""
    ids, box, i = {f.id for f in faces}, _union(faces), _AXIS[normal_axis]
    near = {e.b if e.a in ids else e.a for e in facts.edges if (e.a in ids) != (e.b in ids)} | ids

    def on_outline(c: FaceFacts) -> bool:
        return bool(c.box) and any(abs(c.box[0][k] - box[0][k]) <= TOL or abs(c.box[1][k] - box[1][k]) <= TOL
                                   for k in range(3) if k != i)
    radii = {round(by_id[n].concave_radius, 3) for n in near
             if by_id[n].surface == "cylinder" and by_id[n].concave_radius and by_id[n].concave_axis == normal_axis
             and on_outline(by_id[n])}
    return radii.pop() if len(radii) == 1 else None


def _opens_on_part(facts: PartFacts, floor: FaceFacts, d: str, i: int) -> bool:
    """The hole above this floor runs out to the part's own face on side d."""
    lo, hi = facts.bbox
    walls = [x for x in facts.faces if x.surface == "cylinder" and x.box and x.concave_radius
             and any({e.a, e.b} == {floor.id, x.id} for e in facts.edges)]
    edge = hi[i] if d[0] == "+" else lo[i]
    return bool(walls) and any(abs((x.box[1][i] if d[0] == "+" else x.box[0][i]) - edge) <= TOL for x in walls)


def _floor(facts, faces, by_id) -> str | None:
    """One plane facing a principal direction, below the stock on that side, and walled by concave edges."""
    if len(faces) != 1 or faces[0].surface != "plane" or faces[0].stock or not faces[0].box:
        return None
    f = faces[0]
    d = _principal(f.normal)
    own = [e for e in facts.edges if f.id in (e.a, e.b)]
    if not d or d not in f.access or not any(e.kind == "concave" for e in own):
        return None
    i = _AXIS[d[1]]
    if not _inside(facts, f.box, i):
        return None  # a step or an open slot runs out through a side of the part
    inner = [k for k in range(3) if k != i]
    for n in _neighbours(facts, {f.id}, "convex"):  # only a feature inside the outline may drop away from it
        b = by_id[n].box
        if not b or not all(b[0][k] > f.box[0][k] + TOL and b[1][k] < f.box[1][k] - TOL for k in inner):
            return None  # a shelf: its outline falls away somewhere (Codex, describe review)
    walls = [by_id[n] for n in _neighbours(facts, {f.id}, "concave") if not by_id[n].stock]
    if not walls or any(x.surface == "plane" and _principal(x.normal) is None for x in walls):
        return None  # walls off the principal axes: the box isn't the pocket's size
    depth = _extent(_union(walls) or f.box, i)  # its own walls' height, not the distance from the stock top
    w, h = (_extent(f.box, k) for k in inner)
    r = _corner_radius(faces, facts, by_id, d[1])
    if r and abs(w - 2 * r) <= TOL and abs(h - 2 * r) <= TOL:
        if not _opens_on_part(facts, f, d, i) or not _is_disc(f, r, d[1]):
            return None  # a hole that starts inside a pocket, or a counterbore's shoulder: not described yet
        return f"the bottom of the {_n(depth)} mm deep Ø{_n(2 * r)} hole in the {SIDES[d]} face"
    return f"the floor of {_pocket(w, h, depth, r, d)}"


def _walls(facts, faces, by_id) -> str | None:
    """Planes parallel to one axis joined by concave fillets on it, all reached along that axis: a pocket's sides."""
    cyl = [f for f in faces if f.surface == "cylinder"]
    planes = [f for f in faces if f.surface == "plane"]
    if not cyl or not planes or len(cyl) + len(planes) != len(faces) or any(f.stock for f in faces):
        return None
    axes = {f.concave_axis for f in cyl}
    if len(axes) != 1 or None in axes or any(not f.concave_radius for f in cyl):
        return None
    (axis,) = axes
    i = _AXIS[axis]
    if any(_principal(f.normal) is None or abs(f.normal[i]) > 1e-6 for f in planes):
        return None  # walls off the principal axes: the box isn't the pocket's size
    box = _union(faces)
    opens = [d for d in (f"+{axis}", f"-{axis}") if all(d in f.access for f in faces)]
    if box is None or len(opens) != 1 or len({round(f.concave_radius, 3) for f in cyl}) != 1 \
            or not _inside(facts, box, i):
        return None
    d, r = opens[0], cyl[0].concave_radius
    w, h = (_extent(box, k) for k in range(3) if k != i)
    depth = _extent(box, i)
    if abs(min(w, h) - 2 * r) <= TOL:  # the narrow side is one full radius each end: a round-ended slot
        on = "" if d == "+Z" else f" in the {SIDES[d]} face"
        return f"the walls of the {_n(depth)} mm deep, {_n(min(w, h))} mm wide slot{on}"
    return f"the walls of {_pocket(w, h, depth, r, d)}"


def _pocket(w: float, h: float, depth: float, r: float | None, d: str) -> str:
    """"the 8 mm deep 50 × 30 mm pocket (r6 corners)", with "on the bottom" when it doesn't open upward."""
    on = "" if d == "+Z" else f" on the {SIDES[d]}"
    corners = f" (r{_n(r)} corners)" if r else ""
    return f"the {_n(depth)} mm deep {_n(w)} × {_n(h)} mm pocket{on}{corners}"
