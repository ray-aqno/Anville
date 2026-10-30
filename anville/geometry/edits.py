"""evaluate_edits: edit the world, re-plan, diff (ADR 0002 §2-3; ADR 0003 Amendment 1).

The agent names only an edit kind and a face group. Code proposes the values (from the shop's
tool library and the measured geometry), applies each to a copy of the world, re-plans, and
reports what changed. Every result here is `predicted`: a PartEdit is `measured` only after the
CAD edit is made and the new STEP is re-extracted.

This scores; it doesn't recommend. Every evaluated candidate is returned, flagged with
`changes_plan`, and the caller (the agent's tool layer) decides what to show. A removal that saves
no setup still reaches the agent, for instance when the designer asked for it on intent alone.

Part edits act on a feature: the named group plus the groups sharing a concave edge with it, so
"make this pocket shallower" moves its walls and its floor together, and `remove_feature` drops
all of it (in CAD: suppress the feature that made those faces; ADR 0002 Amendment 1).
"""

import copy
import json
import math
from dataclasses import asdict, dataclass, field, replace

from anville.geometry.facts import DIRECTIONS, PartFacts, face_groups
from anville.geometry.plan import HOLDER_MARGIN, SetupPlan, _Part, min_setups, stickout
from anville.geometry.shop import BACK_CHAMFER_CATALOG, ShopProfile, Tool

CATALOG = ("set_min_radius", "set_depth", "remove_feature", "add_tool", "add_fixture", "route_machine")
NEEDS_GROUP = ("set_min_radius", "set_depth", "remove_feature", "add_tool")


@dataclass
class Summary:
    setups: int
    unreachable: int


@dataclass
class ToolChange:
    face_id: int
    before: str | None  # None: unreachable before
    after: str | None   # None: unreachable after


@dataclass
class Option:
    key: str            # stable within one PartFacts: offered IDs for execute (ADR 0003)
    kind: str
    face_group: str | None
    side: str           # part | shop
    params: dict
    before: Summary
    after: Summary
    tool_changes: list[ToolChange] = field(default_factory=list)
    removed_faces: list[int] = field(default_factory=list)
    changes_plan: bool = False  # setups, reachability or a tool changes; removals alone don't count
    basis: str = "predicted"


@dataclass
class Skipped:
    kind: str
    face_group: str | None
    reason: str


@dataclass
class EvaluateResult:
    options: list[Option]  # plan-changing first, then fewest unreachable, then fewest setups; stable
    skipped: list[Skipped]


def feature_faces(facts: PartFacts, label: str) -> set[int]:
    """The named group plus non-stock groups that share a concave edge with it."""
    group_of = {fid: g for g, ids in facts.labelled_groups().items() for fid in ids}
    stock = {f.id for f in facts.faces if f.stock}
    named = set(facts.group_faces(label))
    out = set(named)
    for e in facts.edges:
        if e.kind != "concave":
            continue
        for a, b in ((e.a, e.b), (e.b, e.a)):
            if a in named and b not in stock:  # one hop from the named group, never from what was added
                out |= set(facts.group_faces(group_of[b])) - stock
    return out


def evaluate_edits(facts: PartFacts, shop: ShopProfile, requests: list[dict], *, axes: int = 3,
                   back_chamfer=()) -> EvaluateResult:
    """Score every candidate value for each `{kind, face_group}` request against the current plan.

    `back_chamfer`: `assess_back_chamfer` results for the shop's cutters and the catalog's
    (RFC 0002). Owned cutters that pass shape the current plan; catalog cutters that pass are
    offered as `add_tool` purchases; catalog cutters that fail say why in `skipped`.
    """
    for req in requests:
        _validate(facts, req)
    base = min_setups(facts, shop, axes=axes, back_chamfer=back_chamfer)
    options, skipped, seen = [], [], set()
    for req in requests:
        kind, label = req["kind"], req.get("face_group")
        if kind == "add_fixture":
            skipped.append(Skipped(kind, label, "fixtures are not modelled yet: workholding is outside PartFacts"))
            continue
        found, duplicates = [], 0
        scope = frozenset(feature_faces(facts, label)) if kind in NEEDS_GROUP else None
        for key, side, params, world in _candidates(facts, shop, axes, base, kind, label, back_chamfer):
            same = (kind, scope, json.dumps(params, sort_keys=True))
            if same in seen:
                duplicates += 1  # another group of the same feature already produced this option
                continue
            seen.add(same)
            found.append(_score(key, kind, label, side, params, facts, base, *world))
        # A catalog cutter that fails is reported even when other add_tool values were found.
        rejected = _catalog_rejections(scope, shop, back_chamfer) if kind == "add_tool" else []
        if found:
            options += found
            if rejected:
                skipped.append(Skipped(kind, label, "; ".join(rejected)))
        elif duplicates:
            skipped.append(Skipped(kind, label, "same feature as an earlier request: its options are listed once"))
        else:
            why = "no candidate values: the shop's tools and the geometry leave none"
            skipped.append(Skipped(kind, label, "; ".join([why, *rejected])))
    options.sort(key=lambda o: (not o.changes_plan, o.after.unreachable, o.after.setups))
    return EvaluateResult(options, skipped)


def _validate(facts: PartFacts, req: dict) -> None:
    if req.get("kind") not in CATALOG:
        raise ValueError(f"edit kind {req.get('kind')!r} is not in the catalog {CATALOG}")
    extra = set(req) - {"kind", "face_group"}
    if extra:
        raise ValueError(f"requests name a kind and a face group; code proposes the values (got {sorted(extra)})")
    if req["kind"] in NEEDS_GROUP and "face_group" not in req:
        raise ValueError(f"{req['kind']} needs a face_group")
    if "face_group" in req:
        facts.group_faces(req["face_group"])


def _candidates(facts, shop, axes, base, kind, label, back_chamfer=()):
    """Yield (key, side, params, (facts', shop', axes', back_chamfer')) for each value worth trying."""
    if kind == "route_machine":
        if axes != 5:
            yield "route_machine/5", "shop", {"axes": 5}, (facts, shop, 5, back_chamfer)
        return
    feature = feature_faces(facts, label)
    part = _Part(facts)
    tools = sorted(shop.tools, key=lambda t: -t.diameter)
    by_id = {f.id: f for f in facts.faces}

    if kind == "set_min_radius":
        # Only corners parallel to an axis a touching face is cut along limit the tool; floor edges don't.
        current = min([f.concave_radius for f in facts.faces if _limiting_fillet(f, feature)] +
                      [e.radius or 0.0 for e in facts.edges if _limiting_corner(e, feature, by_id)],
                      default=math.inf)
        for r in sorted({t.diameter / 2 for t in tools}):
            if r > current:
                yield (f"set_min_radius/{label}/r{r:g}", "part", {"radius": r},
                       (_with_radius(facts, feature, r), shop, axes, back_chamfer))

    elif kind == "set_depth":
        for d in DIRECTIONS:
            depths = [by_id[fid].access[d].depth for fid in feature if d in by_id[fid].access]
            if len(depths) < len(feature) or not depths:
                continue  # the feature isn't opened from d
            deepest = max(depths)
            limit = min(part.limit(by_id[fid], d).diameter for fid in feature)
            # Raising the floor by `shift` lowers every reach step by `shift` (the material around stays put).
            shifts = {max(stickout(by_id[fid], d, t, shop) for fid in feature) - t.reach
                      for t in tools if t.diameter <= limit + 1e-6}
            # Round down: a depth rounded up would shift the floor a hair short of the tool.
            reaches = sorted({math.floor((deepest - s) * 1e4) / 1e4 for s in shifts if s > 1e-6}, reverse=True)
            for depth in reaches:
                params = {"direction": d, "depth": depth}
                yield (f"set_depth/{label}/{d}/{depth:g}", "part", params,
                       (_with_depth(facts, feature, d, deepest - depth), shop, axes, back_chamfer))

    elif kind == "remove_feature":
        yield f"remove_feature/{label}", "part", {}, (_without(facts, feature), shop, axes, back_chamfer)

    elif kind == "add_tool":
        for d in DIRECTIONS:
            faces = [by_id[fid] for fid in feature if d in by_id[fid].access and by_id[fid].access[d].fraction >= 1.0]
            if len(faces) < len(feature):
                continue
            limit = min(part.limit(f, d).diameter for f in faces)
            fitting = [t for t in tools if t.diameter <= limit + 1e-6]
            if not fitting:
                continue  # nothing fits at all
            base_tool = fitting[0]
            depth = max(stickout(f, d, base_tool, shop) for f in faces)
            if depth <= base_tool.reach:
                continue  # an existing tool already reaches
            holder = _slimmer_holder(faces, d, base_tool, shop)
            if holder:
                params = {"type": "holder", "name": base_tool.name, "holder_diameter": holder, "direction": d}
                slim = replace(base_tool, holder_diameter=holder)
                yield (f"add_tool/{label}/{d}/{base_tool.name}-H{holder:g}", "shop", params,
                       (facts, replace(shop, tools=[slim if t is base_tool else t for t in shop.tools]), axes,
                        back_chamfer))
            max_ld = math.ceil(depth / base_tool.diameter)
            tool = replace(base_tool, name=_unique_name(f"{base_tool.name}-L{base_tool.diameter * max_ld:g}", shop),
                           max_ld=max_ld)
            params = {"name": tool.name, "diameter": tool.diameter, "max_ld": tool.max_ld, "direction": d}
            yield f"add_tool/{label}/{d}/{tool.name}", "shop", params, \
                (facts, replace(shop, tools=[*shop.tools, tool]), axes, back_chamfer)
        jaw_axis = {c.face_id: c.axis for c in facts.edge_chamfers}
        for access in _catalog_passes(feature, shop, back_chamfer):
            cutter = _CATALOG[access.tool]
            # The reliefs were checked for this chamfer's jaw axis: score the plan with those jaws only.
            same_jaws = [a for a in back_chamfer if jaw_axis.get(a.face_id) == jaw_axis.get(access.face_id)]
            params = {"type": "back_chamfer", "name": cutter.name, "angle": cutter.angle,
                      "head_diameter": cutter.head_diameter, "neck_diameter": cutter.neck_diameter,
                      "neck_length": cutter.neck_length, "length_of_cut": cutter.length_of_cut,
                      "direction": "+Z", "owned": False, "fixture": "relieved soft jaws",
                      "reliefs": [asdict(r) for r in access.reliefs]}
            yield (f"add_tool/{label}/+Z/BC{cutter.angle:g}-{cutter.name}", "shop", params,
                   (facts, replace(shop, back_chamfer_tools=[*shop.back_chamfer_tools, cutter]), axes, same_jaws))


_CATALOG = {c.name: c for c in BACK_CHAMFER_CATALOG}


def _catalog_passes(feature: set[int], shop: ShopProfile, back_chamfer) -> list:
    """Catalog cutters, not yet owned, that pass checks a-f on a chamfer of this feature: one per cutter."""
    owned = {t.name for t in shop.back_chamfer_tools}
    out, seen = [], set()
    for a in back_chamfer:
        if a.face_id in feature and a.ok and a.tool in _CATALOG and a.tool not in owned and a.tool not in seen:
            seen.add(a.tool)
            out.append(a)
    return out


def _catalog_rejections(feature, shop: ShopProfile, back_chamfer) -> list[str]:
    """Why each catalog cutter can't finish a chamfer of this feature, check by check."""
    owned = {t.name for t in shop.back_chamfer_tools}
    return [f"{a.tool} fails check {c.name}: {c.detail}"
            for a in back_chamfer if feature and a.face_id in feature and a.tool in _CATALOG
            and a.tool not in owned for c in a.checks if not c.passed]


def _slimmer_holder(faces, d: str, tool: Tool, shop: ShopProfile) -> int | None:
    """The widest whole-mm holder, slimmer than the current one and no slimmer than the tool, that lets
    `tool` reach every face from d (ADR 0008 Amendment 1): its sweep must stop short of each face's first
    reach step that needs more than the tool's reach."""
    stops = [c for f in faces for c, s in (f.access[d].reach or [[0.0, f.access[d].depth]]) if s > tool.reach + 1e-6]
    if not stops or min(stops) <= 0.0:
        return None  # the tool can't reach even right beside the face
    widest = math.ceil(2 * (min(stops) - HOLDER_MARGIN + tool.diameter / 2)) - 1  # sweep strictly below the stop
    return widest if tool.diameter <= widest < shop.holder(tool) else None


def _unique_name(name: str, shop: ShopProfile) -> str:
    """Plans record tools by name only, so a proposed tool must not reuse one already in the shop."""
    taken = {t.name for t in shop.tools}
    out, n = name, 2
    while out in taken:
        out, n = f"{name}-{n}", n + 1
    return out


def _cut_axes(face) -> set[str]:
    """Axes of the directions this face is fully visible from."""
    return {d[1] for d, a in face.access.items() if a.fraction >= 1.0}


def _limiting_fillet(face, feature: set[int]) -> bool:
    return face.id in feature and face.concave_radius is not None and face.concave_axis in _cut_axes(face)


def _limiting_corner(e, feature: set[int], by_id: dict) -> bool:
    """A concave straight edge of the feature, parallel to an axis that a face it touches is cut along."""
    return (e.kind == "concave" and e.axis is not None and (e.a in feature or e.b in feature)
            and e.axis in _cut_axes(by_id[e.a]) | _cut_axes(by_id[e.b]))


def _with_radius(facts: PartFacts, feature: set[int], r: float) -> PartFacts:
    """Raise the feature's tool-limiting corner radii to at least r; sharp corners get filleted."""
    new = copy.deepcopy(facts)
    by_id = {f.id: f for f in facts.faces}
    for f in new.faces:
        if _limiting_fillet(f, feature) and f.concave_radius < r:
            f.concave_radius = r
    new.edges = [replace(e, radius=r) if _limiting_corner(e, feature, by_id) and (e.radius or 0.0) < r else e
                 for e in new.edges]
    return new


def _without(facts: PartFacts, feature: set[int]) -> PartFacts:
    """Drop the feature's faces; the stock faces it was cut from close over the gap."""
    new = copy.deepcopy(facts)
    new.faces = [f for f in new.faces if f.id not in feature]
    new.edges = [e for e in new.edges if e.a not in feature and e.b not in feature]
    new.groups = face_groups(new.faces, new.edges)
    return new


def _with_depth(facts: PartFacts, feature: set[int], d: str, shift: float) -> PartFacts:
    """Raise the feature's floor by `shift` along d."""
    new = copy.deepcopy(facts)
    for f in new.faces:
        if f.id in feature:
            a = f.access[d]
            a.depth = max(0.0, a.depth - shift)
            if a.reach:
                a.reach = [[c, max(0.0, s - shift)] for c, s in a.reach]
    return new


def _score(key, kind, label, side, params, facts, base: SetupPlan, new_facts, new_shop, new_axes,
           new_back_chamfer) -> Option:
    after = min_setups(new_facts, new_shop, axes=new_axes, back_chamfer=new_back_chamfer)
    t0, t1 = _tools(base), _tools(after)
    present = {f.id for f in new_facts.faces}
    changes = [ToolChange(fid, t0.get(fid), t1.get(fid)) for fid in sorted((t0.keys() | t1.keys()) & present)
               if t0.get(fid) != t1.get(fid)]
    return Option(key=key, kind=kind, face_group=label, side=side, params=params,
                  before=Summary(base.count, len(base.unreachable)),
                  after=Summary(after.count, len(after.unreachable)),
                  tool_changes=changes,
                  removed_faces=sorted({f.id for f in facts.faces} - present),
                  changes_plan=(after.count, len(after.unreachable)) != (base.count, len(base.unreachable))
                  or bool(changes))


def _tools(plan: SetupPlan) -> dict[int, str]:
    return {a.face_id: a.tool for s in plan.setups for a in s.faces}
