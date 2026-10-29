"""min_setups: exact setup search over the 6 principal directions (ADR 0002 §3, ADR 0008 §5-7).

Pure Python over PartFacts, so PartEdits and ledger re-plans run without OCP.
"""

import itertools
import math
from dataclasses import asdict, dataclass, field

from anville.geometry.facts import DIRECTIONS, FaceFacts, PartFacts
from anville.geometry.shop import ShopProfile, Tool

FIT_TOL = 1e-6  # mm
# 5-axis fixture sides to try, part resting on its bottom first.
FIXTURE_ORDER = ("-Z", "+Z", "-X", "+X", "-Y", "+Y")


@dataclass
class Assignment:
    face_id: int
    tool: str  # largest tool that finishes the face from this setup


@dataclass
class Setup:
    directions: list[str]  # one on a 3-axis mill; several for one 3+2 fixturing
    faces: list[Assignment] = field(default_factory=list)


@dataclass
class Unreachable:
    face_id: int
    reason: str  # no_direction | no_tool
    detail: str


@dataclass
class SetupPlan:
    axes: int
    setups: list[Setup]
    unreachable: list[Unreachable]

    @property
    def count(self) -> int:
        return len(self.setups)

    def to_dict(self) -> dict:
        return {**asdict(self), "count": self.count}


@dataclass
class _Limit:
    diameter: float  # largest tool diameter that fits, math.inf if unconstrained
    cause: str       # what sets it: corner radius, wall gap, or a sharp inside corner


class _Part:
    """Indexes PartFacts for limit lookups."""

    def __init__(self, facts: PartFacts):
        self.faces = {f.id: f for f in facts.faces}
        self.group = {fid: g for g in facts.groups for fid in g}
        self.neighbours: dict[int, set[int]] = {f.id: set() for f in facts.faces}
        # Concave straight edges along a principal axis: axis -> tightest radius (0.0 when sharp).
        self.corners: dict[int, dict[str, float]] = {f.id: {} for f in facts.faces}
        for e in facts.edges:
            if e.kind in ("concave", "tangent"):
                self.neighbours[e.a].add(e.b)
                self.neighbours[e.b].add(e.a)
            if e.kind == "concave" and e.axis:
                r = e.radius or 0.0
                for fid in (e.a, e.b):
                    self.corners[fid][e.axis] = min(r, self.corners[fid].get(e.axis, math.inf))

    def limit(self, face: FaceFacts, d: str) -> _Limit:
        """Largest tool diameter that can finish `face` from `d`: tightest over its group and concave neighbours."""
        group = self.group.get(face.id, [face.id])
        corner = min((self.corners[g].get(d[1], math.inf) for g in group), default=math.inf)
        if corner == 0.0:
            return _Limit(0.0, f"sharp inside corner parallel to {d[1]}")
        best = _Limit(2 * corner, f"r{corner:g} corner parallel to {d[1]}") if corner < math.inf else _Limit(math.inf, "")
        region = set(group).union(*(self.neighbours[g] for g in group))
        for fid in sorted(region):
            g = self.faces[fid]
            if g.concave_axis == d[1] and g.concave_radius is not None and 2 * g.concave_radius < best.diameter:
                best = _Limit(2 * g.concave_radius, f"r{g.concave_radius:g} corner on face {fid}")
            a = g.access.get(d)
            if a and a.gap is not None and a.gap < best.diameter:
                best = _Limit(a.gap, f"{a.gap:.3g} mm gap at face {fid}")
        return best


def _fit(part: _Part, face: FaceFacts, d: str, tools: list[Tool]) -> tuple[Tool | None, str]:
    """The largest tool that finishes `face` from `d`, or None with the reason."""
    a = face.access.get(d)
    if a is None or a.fraction < 1.0:
        return None, ""
    lim = part.limit(face, d)
    for tool in tools:
        if tool.diameter <= lim.diameter + FIT_TOL and a.depth <= tool.reach + FIT_TOL:
            return tool, ""
    width = "any diameter" if math.isinf(lim.diameter) else f"Ø≤{lim.diameter:.3g} ({lim.cause})"
    return None, f"{d}: needs {width} reaching {a.depth:.3g} mm"


def min_setups(facts: PartFacts, shop: ShopProfile, *, axes: int = 3, back_chamfer=()) -> SetupPlan:
    """Fewest setups that finish every reachable face; unreachable faces come back with a reason.

    `back_chamfer` takes the results of `assess_back_chamfer` (RFC 0002): a chamfer whose checks a-f
    all pass, for a cutter this shop owns, can also be finished from +Z with that cutter. The jaws
    grip across the chamfered edge, so one setup back-chamfers along one edge axis only: each axis is
    tried and the plan that finishes the most faces wins, then the one with the fewest setups (ties go to X).
    """
    if axes not in (3, 5):
        raise ValueError(f"axes must be 3 or 5, got {axes}")
    owned = {t.name: t for t in shop.back_chamfer_tools}
    from_top = {a.face_id: owned[a.tool] for a in back_chamfer if a.ok and a.tool in owned}
    jaw_axis = {c.face_id: c.axis for c in facts.edge_chamfers}
    options = sorted({jaw_axis.get(fid, "") for fid in from_top})
    if len(options) <= 1:
        return _plan(facts, shop, axes, from_top)
    plans = [_plan(facts, shop, axes, {fid: t for fid, t in from_top.items() if jaw_axis.get(fid, "") == ax})
             for ax in options]
    return min(plans, key=lambda p: (len(p.unreachable), p.count))  # a shorter plan that skips a face is worse


def _plan(facts: PartFacts, shop: ShopProfile, axes: int, from_top: dict) -> SetupPlan:
    part = _Part(facts)
    tools = sorted(shop.tools, key=lambda t: -t.diameter)
    fits: dict[str, dict[int, Tool]] = {d: {} for d in DIRECTIONS}
    unreachable = []
    for face in facts.faces:
        if face.stock:
            continue
        misses = []
        for d in DIRECTIONS:
            tool, why = _fit(part, face, d, tools)
            if tool:
                fits[d][face.id] = tool
            elif why:
                misses.append(why)
        if face.id in from_top:
            cutter = from_top[face.id]
            fits["+Z"][face.id] = Tool(cutter.name, cutter.head_diameter, cutter.neck_length / cutter.head_diameter)
        if not any(face.id in fits[d] for d in DIRECTIONS):
            if misses:
                unreachable.append(Unreachable(face.id, "no_tool", "; ".join(misses)))
            else:
                unreachable.append(Unreachable(face.id, "no_direction",
                                               "not fully visible from any of the 6 principal directions"))
    universe = set().union(*(fits[d].keys() for d in DIRECTIONS))

    if axes == 3:
        dirs = _min_cover(universe, fits, DIRECTIONS) or ("+Z",)
        groups = [[d] for d in dirs]
    else:
        groups = _five_axis(universe, fits)

    setups, done = [], set()
    for group in groups:
        setup = Setup(directions=list(group))
        for d in group:
            for fid in sorted(fits[d].keys() - done):
                setup.faces.append(Assignment(fid, fits[d][fid].name))
                done.add(fid)
        setups.append(setup)
    return SetupPlan(axes=axes, setups=setups, unreachable=unreachable)


def _min_cover(universe: set[int], fits: dict, allowed: tuple[str, ...]) -> tuple[str, ...] | None:
    """Smallest subset of `allowed` directions covering `universe`; ties go to DIRECTIONS order."""
    for size in range(len(allowed) + 1):
        for subset in itertools.combinations(allowed, size):
            if universe <= set().union(*(fits[d].keys() for d in subset)):
                return subset
    return None


def _five_axis(universe: set[int], fits: dict) -> list[tuple[str, ...]]:
    """3+2 indexing: one fixturing reaches every direction except the side it is clamped on."""
    for fixture in FIXTURE_ORDER:
        cover = _min_cover(universe, fits, tuple(d for d in DIRECTIONS if d != fixture))
        if cover is not None:
            return [cover or ("+Z",)]
    # Every side is needed: clamp on the bottom for everything else, then flip to finish the bottom.
    others = tuple(d for d in DIRECTIONS if d != "-Z")
    first = _min_cover(universe & set().union(*(fits[d].keys() for d in others)), fits, others)
    return [first, ("-Z",)]
