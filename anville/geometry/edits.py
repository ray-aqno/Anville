"""evaluate_edits: edit the world, re-plan, diff (ADR 0002 §2-3; ADR 0003 Amendment 1).

The agent names only an edit kind and a face group. Code proposes the values (from the shop's
tool library and the measured geometry), applies each to a copy of the world, re-plans, and
reports what changed. Every result here is `predicted`: a PartEdit is `measured` only after the
CAD edit is made and the new STEP is re-extracted.

Part edits act on a feature: the named group plus the groups sharing a concave edge with it, so
"make this pocket shallower" moves its walls and its floor together.
"""

import copy
import math
from dataclasses import dataclass, field, replace

from anville.geometry.facts import DIRECTIONS, PartFacts, face_groups
from anville.geometry.plan import SetupPlan, _Part, min_setups
from anville.geometry.shop import ShopProfile, Tool

CATALOG = ("set_min_radius", "set_depth", "add_tool", "add_fixture", "route_machine")
NEEDS_GROUP = ("set_min_radius", "set_depth", "add_tool")


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
    basis: str = "predicted"


@dataclass
class Skipped:
    kind: str
    face_group: str | None
    reason: str


@dataclass
class EvaluateResult:
    options: list[Option]  # fewest unreachable, then fewest setups; ties keep request order
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


def evaluate_edits(facts: PartFacts, shop: ShopProfile, requests: list[dict], *, axes: int = 3) -> EvaluateResult:
    """Score every candidate value for each `{kind, face_group}` request against the current plan."""
    for req in requests:
        _validate(facts, req)
    base = min_setups(facts, shop, axes=axes)
    options, skipped, seen = [], [], set()
    for req in requests:
        kind, label = req["kind"], req.get("face_group")
        if kind == "add_fixture":
            skipped.append(Skipped(kind, label, "fixtures are not modelled yet: workholding is outside PartFacts"))
            continue
        found, duplicates = [], 0
        scope = frozenset(feature_faces(facts, label)) if kind in NEEDS_GROUP else None
        for key, side, params, world in _candidates(facts, shop, axes, base, kind, label):
            same = (kind, scope, tuple(sorted(params.items())))
            if same in seen:
                duplicates += 1  # another group of the same feature already produced this option
                continue
            seen.add(same)
            option = _score(key, kind, label, side, params, facts, base, *world)
            # Offer only what changes the computed plan (ADR 0002: never recommend a change that saves
            # nothing). Cycle time isn't modelled yet, so a removal that only saves machining time has
            # no number behind it; once Summary carries dollars, it passes this check on its own.
            if option.after != option.before or option.tool_changes:
                found.append(option)
        if found:
            options += found
        elif duplicates:
            skipped.append(Skipped(kind, label, "same feature as an earlier request: its options are listed once"))
        else:
            skipped.append(Skipped(kind, label, "no candidate value changes the plan"))
    options.sort(key=lambda o: (o.after.unreachable, o.after.setups))
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


def _candidates(facts, shop, axes, base, kind, label):
    """Yield (key, side, params, (facts', shop', axes')) for each value worth trying."""
    if kind == "route_machine":
        if axes != 5:
            yield "route_machine/5", "shop", {"axes": 5}, (facts, shop, 5)
        return
    feature = feature_faces(facts, label)
    part = _Part(facts)
    tools = sorted(shop.tools, key=lambda t: -t.diameter)
    by_id = {f.id: f for f in facts.faces}

    if kind == "set_min_radius":
        current = min([f.concave_radius for f in facts.faces if f.id in feature and f.concave_radius] +
                      [e.radius or 0.0 for e in facts.edges
                       if e.kind == "concave" and e.axis and (e.a in feature or e.b in feature)], default=math.inf)
        for r in sorted({t.diameter / 2 for t in tools}):
            if r > current:
                yield f"set_min_radius/{label}/r{r:g}", "part", {"radius": r}, (_with_radius(facts, feature, r), shop, axes)

    elif kind == "set_depth":
        for d in DIRECTIONS:
            depths = [by_id[fid].access[d].depth for fid in feature if d in by_id[fid].access]
            if len(depths) < len(feature) or not depths:
                continue  # the feature isn't opened from d
            deepest = max(depths)
            limit = min(part.limit(by_id[fid], d).diameter for fid in feature)
            reaches = sorted({t.reach for t in tools if t.diameter <= limit + 1e-6 and t.reach < deepest}, reverse=True)
            for depth in reaches + [0.0]:
                params = {"direction": d, "depth": depth}
                yield (f"set_depth/{label}/{d}/{depth:g}", "part", params,
                       (_with_depth(facts, feature, d, deepest - depth), shop, axes))

    elif kind == "add_tool":
        for d in DIRECTIONS:
            faces = [by_id[fid] for fid in feature if d in by_id[fid].access and by_id[fid].access[d].fraction >= 1.0]
            if len(faces) < len(feature):
                continue
            limit = min(part.limit(f, d).diameter for f in faces)
            depth = max(f.access[d].depth for f in faces)
            fitting = [t for t in tools if t.diameter <= limit + 1e-6]
            if not fitting or depth <= fitting[0].reach:
                continue  # nothing fits at all, or an existing tool already reaches
            base_tool = fitting[0]
            max_ld = math.ceil(depth / base_tool.diameter)
            tool = Tool(_unique_name(f"{base_tool.name}-L{base_tool.diameter * max_ld:g}", shop),
                        base_tool.diameter, max_ld)
            params = {"name": tool.name, "diameter": tool.diameter, "max_ld": tool.max_ld, "direction": d}
            yield f"add_tool/{label}/{d}/{tool.name}", "shop", params, \
                (facts, ShopProfile(list(shop.tools) + [tool]), axes)


def _unique_name(name: str, shop: ShopProfile) -> str:
    """Plans record tools by name only, so a proposed tool must not reuse one already in the shop."""
    taken = {t.name for t in shop.tools}
    out, n = name, 2
    while out in taken:
        out, n = f"{name}-{n}", n + 1
    return out


def _with_radius(facts: PartFacts, feature: set[int], r: float) -> PartFacts:
    new = copy.deepcopy(facts)
    for f in new.faces:
        if f.id in feature and f.concave_radius is not None and f.concave_radius < r:
            f.concave_radius = r
    new.edges = [replace(e, radius=r) if e.kind == "concave" and e.axis and (e.a in feature or e.b in feature)
                 and (e.radius or 0.0) < r else e for e in new.edges]
    return new


def _with_depth(facts: PartFacts, feature: set[int], d: str, shift: float) -> PartFacts:
    """Raise the feature's floor by `shift` along d; a shift to zero depth removes the feature."""
    new = copy.deepcopy(facts)
    deepest = max(f.access[d].depth for f in new.faces if f.id in feature)
    if shift >= deepest - 1e-9:
        new.faces = [f for f in new.faces if f.id not in feature]
        new.edges = [e for e in new.edges if e.a not in feature and e.b not in feature]
        new.groups = face_groups(new.faces, new.edges)
        return new
    for f in new.faces:
        if f.id in feature:
            f.access[d].depth = max(0.0, f.access[d].depth - shift)
    return new


def _score(key, kind, label, side, params, facts, base: SetupPlan, new_facts, new_shop, new_axes) -> Option:
    after = min_setups(new_facts, new_shop, axes=new_axes)
    t0, t1 = _tools(base), _tools(after)
    present = {f.id for f in new_facts.faces}
    changes = [ToolChange(fid, t0.get(fid), t1.get(fid)) for fid in sorted((t0.keys() | t1.keys()) & present)
               if t0.get(fid) != t1.get(fid)]
    return Option(key=key, kind=kind, face_group=label, side=side, params=params,
                  before=Summary(base.count, len(base.unreachable)),
                  after=Summary(after.count, len(after.unreachable)),
                  tool_changes=changes,
                  removed_faces=sorted({f.id for f in facts.faces} - present))


def _tools(plan: SetupPlan) -> dict[int, str]:
    return {a.face_id: a.tool for s in plan.setups for a in s.faces}
