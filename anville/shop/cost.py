"""cost_options: dollars per option at fixed quantities, decided over every corner of the input box (RFC 0002 §4).

    cost(route, q) = one_time
                   + batches(q) × (setups × setup_time × rate + fee)
                   + q × (setups × load_time + cycle_time + chamfers × back_chamfer_pass) × rate

Every term multiplies distinct inputs, so a route's cost, and the difference between two routes, is
multiaffine: linear in each input with the others held. Its minimum and maximum over the box of input
ranges sit at the box's corners, so evaluating every corner is exact, not a sample.

A route is recommended at a quantity only if it is cheaper than every other complete route at every
corner. Otherwise the verdict names the leader (cheapest with each input at its midpoint) and the corner
where it loses by the most: the combination of rates, times and prices that flips it.

The caller filters options first (stated intent, ADR 0002 Amendment 1): this prices what it is given.
"""

import itertools
import math
from dataclasses import asdict, dataclass, field, fields

from anville.geometry.edits import Option
from anville.geometry.facts import PartFacts
from anville.geometry.plan import SetupPlan
from anville.shop.profile import Input, Shop, ShopCosts

CURRENT = "current"
PART_EDITS = ("set_min_radius", "set_depth", "remove_feature")
TOOLS = ("long_reach_price",)  # with every "tool_price.<name>": bought tools, shared by Scenarios.tool_parts
JAWS = ("jaw_blank", "jaw_machining")  # each part's own relieved jaws, wherever its plan back-chamfers
TOL = 1e-9  # dollars


@dataclass
class Route:
    key: str          # the option's key, or "current" for the plan as it stands
    side: str         # current | part | shop | outsource
    setups: int
    complete: bool    # False when the plan leaves faces unreachable: never recommended
    machine: str      # mill (in-house 3-axis) | outsource (an outside 5-axis shop)
    buys: list[str]   # inputs paid once: tool prices, jaw blanks, jaw machining time
    chamfers: int     # faces finished with a back-chamfer cutter, each paying back_chamfer_pass
    one_time: tuple[float, float] = (0.0, 0.0)              # $, min and max over the corners
    costs: dict[int, tuple[float, float]] = field(default_factory=dict)  # quantity -> $, min and max
    # Parts from which a tool purchase stays no dearer than the current plan, min and max over the corners.
    # None if nothing is bought; an end is None where buying never stays cheaper, at any quantity.
    payback: tuple[int | None, int | None] | None = None
    payback_mid: int | None = None  # the same with every input at its midpoint: one number, shown with the range
    unpriced: list[str] = field(default_factory=list)  # what the dollars leave out


@dataclass
class Verdict:
    quantity: int
    recommended: str | None  # cheapest at every corner, among complete routes
    leader: str              # cheapest with every ranged input at its midpoint
    flip: dict | None        # {"corner", "winner", "by"}: where the leader loses by most; None if recommended


@dataclass
class CostResult:
    part: str                 # source_sha256 of the part priced
    routes: list[Route]
    verdicts: list[Verdict]
    inputs: dict[str, dict]   # every input: lo, hi, unit, status, source
    scenarios: dict

    def to_dict(self) -> dict:
        return asdict(self)


def cost_options(facts: PartFacts, shop: Shop, options: list[Option], *, plan: SetupPlan) -> CostResult:
    """Price the current plan and each option over every corner of the shop's input ranges."""
    costs = getattr(shop, "costs", None)
    if not isinstance(costs, ShopCosts):
        raise ValueError("this shop profile has no cost inputs: use anville.shop.Shop")
    cutters = {t.name for t in shop.back_chamfer_tools}
    on_cutter = {a.face_id for s in plan.setups for a in s.faces if a.tool in cutters}
    # The shop has no 5-axis mill: a 5-axis plan goes out, like route_machine/5 (Codex, PR #20).
    if plan.axes == 5:
        current = Route(CURRENT, CURRENT, plan.count, not plan.unreachable, "outsource", [], 0)
    else:
        current = Route(CURRENT, CURRENT, plan.count, not plan.unreachable, "mill", _jaws(on_cutter), len(on_cutter))
    routes = [current] + [_route(o, costs, cutters, on_cutter) for o in options]

    ranged = _ranged(costs)
    corners = list(_corners(ranged))
    mid = {name: (i.lo + i.hi) / 2 for name, i in ranged.items()}
    for r in routes:
        one = [_one_time(r, c, costs) for c in corners]
        r.one_time = (round(min(one), 2), round(max(one), 2))
        for q in costs.scenarios.quantities:
            v = [_route_cost(r, c, q, costs) for c in corners]
            r.costs[q] = (round(min(v), 2), round(max(v), 2))
        if any(_is_tool(n) for n in r.buys):
            paid = [_payback_at(r, routes[0], c, costs) for c in corners]
            known = [p for p in paid if p is not None]
            r.payback = (min(known), None if None in paid else max(known)) if known else (None, None)
            r.payback_mid = _payback_at(r, routes[0], mid, costs)

    complete = [r for r in routes if r.complete]
    verdicts = [_verdict(complete, corners, mid, q, costs) for q in costs.scenarios.quantities]
    return CostResult(facts.source_sha256, routes, verdicts,
                      {name: asdict(i) for name, i in _inputs(costs).items()}, asdict(costs.scenarios))


def _route(o: Option, costs: ShopCosts, cutters: set[str], on_cutter: set[int]) -> Route:
    """How an option changes what the shop pays: machine, one-time buys, back-chamfered faces."""
    bought = o.params.get("name") if o.params.get("type") == "back_chamfer" else None
    names = cutters | {bought} if bought else cutters
    faces = set(on_cutter)
    for c in o.tool_changes:
        (faces.add if c.after in names else faces.discard)(c.face_id)
    faces -= set(o.removed_faces)
    complete = o.after.unreachable == 0
    if o.kind in PART_EDITS:
        return Route(o.key, "part", o.after.setups, complete, "mill", _jaws(faces), len(faces),
                     unpriced=["the designer's time to make and check the CAD edit"])
    if o.kind == "route_machine":
        return Route(o.key, "outsource", o.after.setups, complete, "outsource", [], 0,
                     unpriced=["lead time to and from the outside shop"])
    if o.kind == "add_tool" and bought:
        if bought not in costs.tool_price:
            raise ValueError(f"no price for catalog tool {bought!r} in the shop profile")
        return Route(o.key, "shop", o.after.setups, complete, "mill",
                     [f"tool_price.{bought}", *_jaws(faces)], len(faces))
    if o.kind == "add_tool":
        return Route(o.key, "shop", o.after.setups, complete, "mill", ["long_reach_price", *_jaws(faces)], len(faces))
    raise ValueError(f"cost_options can't price a {o.kind!r} option")


def _jaws(back_chamfered) -> list[str]:
    """A plan that back-chamfers holds this part in its own relieved jaws: re-cut for each stock size (RFC 0002 §3)."""
    return list(JAWS) if back_chamfered else []


def _is_tool(name: str) -> bool:
    return name in TOOLS or name.startswith("tool_price.")


# ---- inputs and corners -------------------------------------------------------------------

def _inputs(costs: ShopCosts) -> dict[str, Input]:
    out = {f.name: getattr(costs, f.name) for f in fields(costs) if isinstance(getattr(costs, f.name), Input)}
    out.update({f"tool_price.{name}": i for name, i in costs.tool_price.items()})
    return out


def _ranged(costs: ShopCosts) -> dict[str, Input]:
    """Inputs with a real range: each doubles the corners. Pinned inputs add none."""
    return {name: i for name, i in _inputs(costs).items() if i.lo < i.hi}


def _corners(ranged: dict[str, Input]):
    names = sorted(ranged)
    for ends in itertools.product(*((ranged[n].lo, ranged[n].hi) for n in names)):
        yield dict(zip(names, ends))


def _getter(point: dict, costs: ShopCosts):
    inputs = _inputs(costs)
    return lambda name: point[name] if name in point else inputs[name].lo


# ---- dollars -------------------------------------------------------------------------------

def _one_time(r: Route, point: dict, costs: ShopCosts) -> float:
    """Tools are shared by the part designs they serve; jaws and their machining belong to this part."""
    get, inputs = _getter(point, costs), _inputs(costs)
    share = 1.0 / costs.scenarios.tool_parts
    return sum(get(n) * (get("mill_rate") if inputs[n].unit == "hr" else 1.0) * (share if _is_tool(n) else 1.0)
               for n in r.buys)


def _terms(r: Route, point: dict, costs: ShopCosts) -> tuple[float, float, float]:
    """A route's dollars at one point as (one-time, per batch, per part)."""
    get = _getter(point, costs)
    rate = get("mill_rate") if r.machine == "mill" else get("outsource_rate")
    fee = get("outsource_fee") if r.machine == "outsource" else 0.0
    per_part = r.setups * get("load_time") + get("cycle_time") + r.chamfers * get("back_chamfer_pass")
    return _one_time(r, point, costs), r.setups * get("setup_time") * rate + fee, per_part * rate


def _route_cost(r: Route, point: dict, q: int, costs: ShopCosts) -> float:
    return _at(_terms(r, point, costs), q, costs.scenarios.batch_size)


def _at(terms: tuple[float, float, float], q: int, batch_size: int) -> float:
    """Dollars for q parts from a route's (one-time, per batch, per part) terms."""
    one, per_batch, per_part = terms
    return one + math.ceil(q / batch_size) * per_batch + q * per_part


def _payback_at(r: Route, current: Route, point: dict, costs: ShopCosts) -> int | None:
    """The quantity from which buying stays no dearer than the current plan, at one point; None if it never does.

    Not the first such quantity: when the pass costs more than the load it saves, savings fall within a
    batch and rise at the next one's saved setup, so buying can lead and then trail again.

    buy − current at q parts is gap(q) = d_one + ceil(q/n)·d_batch + q·d_part. Batch k's worst point (its
    last part if d_part > 0, else its first) is d_one + k·s + extra, with s = d_batch + n·d_part. If s > 0
    buying ends up dearer at any quantity (Codex, PR #20: a scan to 1,000 parts missed that). Otherwise batch
    worsts fall, so the first batch K whose worst is ≤ 0 is computed, and the last losing quantity lies just
    below it.
    """
    (o1, b1, p1), (o0, b0, p0) = _terms(r, point, costs), _terms(current, point, costs)
    n = costs.scenarios.batch_size
    d_one, d_batch, d_part = o1 - o0, b1 - b0, p1 - p0
    s, extra = d_batch + n * d_part, max(0.0, -(n - 1) * d_part)
    if s > TOL or (s >= -TOL and d_one + extra > TOL):
        return None
    if d_one + extra <= TOL:
        k = 1
    else:
        k = math.ceil((d_one + extra) / -s)
    for q in range(k * n, 0, -1):  # every batch from K on stays ahead; the last loss is in batch K - 1
        if d_one + math.ceil(q / n) * d_batch + q * d_part > TOL:
            return q + 1
    return 1


def _verdict(routes: list[Route], corners: list[dict], mid: dict, q: int, costs: ShopCosts) -> Verdict:
    if not routes:
        return Verdict(q, None, CURRENT, None)
    leader = min(routes, key=lambda r: _route_cost(r, mid, q, costs))
    worst = None  # (by, corner, winner): the corner where the leader trails the cheapest other route most
    for c in corners:
        mine = _route_cost(leader, c, q, costs)
        rival = min((r for r in routes if r is not leader), key=lambda r: _route_cost(r, c, q, costs), default=None)
        if rival is None:
            break
        by = mine - _route_cost(rival, c, q, costs)
        if by >= 0 and (worst is None or by > worst[0]):
            worst = (by, c, rival.key)
    if worst is None:
        return Verdict(q, leader.key, leader.key, None)
    by, corner, winner = worst
    return Verdict(q, None, leader.key, {"corner": {k: round(v, 4) for k, v in corner.items()},
                                          "winner": winner, "by": round(by, 2)})
