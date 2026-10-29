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
PAYBACK_CAP = 1000  # parts; a purchase that hasn't paid back by then is reported as open-ended
TOOLS = ("long_reach_price",)  # with every "tool_price.<name>": bought tools, shared by Scenarios.tool_parts


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
    payback: tuple[int, int | None] | None = None  # parts until a purchase beats the current plan; None if no purchase
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
    routes = [Route(CURRENT, CURRENT, plan.count, not plan.unreachable, "mill", [], len(on_cutter))]
    routes += [_route(o, costs, cutters, on_cutter) for o in options]

    ranged = _ranged(costs)
    corners = list(_corners(ranged))
    for r in routes:
        one = [_one_time(r, c, costs) for c in corners]
        r.one_time = (round(min(one), 2), round(max(one), 2))
        for q in costs.scenarios.quantities:
            v = [_route_cost(r, c, q, costs) for c in corners]
            r.costs[q] = (round(min(v), 2), round(max(v), 2))
        if r.buys:
            paid = [_payback_at(r, routes[0], c, costs) for c in corners]
            known = [p for p in paid if p is not None]
            r.payback = (min(known), None if None in paid else max(known)) if known else (PAYBACK_CAP, None)

    complete = [r for r in routes if r.complete]
    mid = {name: (i.lo + i.hi) / 2 for name, i in ranged.items()}
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
        return Route(o.key, "part", o.after.setups, complete, "mill", [], len(faces),
                     unpriced=["the designer's time to make and check the CAD edit"])
    if o.kind == "route_machine":
        return Route(o.key, "outsource", o.after.setups, complete, "outsource", [], 0,
                     unpriced=["lead time to and from the outside shop"])
    if o.kind == "add_tool" and bought:
        if bought not in costs.tool_price:
            raise ValueError(f"no price for catalog tool {bought!r} in the shop profile")
        return Route(o.key, "shop", o.after.setups, complete, "mill",
                     [f"tool_price.{bought}", "jaw_blank", "jaw_machining"], len(faces))
    if o.kind == "add_tool":
        return Route(o.key, "shop", o.after.setups, complete, "mill", ["long_reach_price"], len(faces))
    raise ValueError(f"cost_options can't price a {o.kind!r} option")


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
    return sum(get(n) * (get("mill_rate") if inputs[n].unit == "hr" else 1.0) * (share if n in TOOLS or
               n.startswith("tool_price.") else 1.0) for n in r.buys)


def _route_cost(r: Route, point: dict, q: int, costs: ShopCosts) -> float:
    get = _getter(point, costs)
    rate = get("mill_rate") if r.machine == "mill" else get("outsource_rate")
    fee = get("outsource_fee") if r.machine == "outsource" else 0.0
    batches = math.ceil(q / costs.scenarios.batch_size)
    per_part = r.setups * get("load_time") + get("cycle_time") + r.chamfers * get("back_chamfer_pass")
    return _one_time(r, point, costs) + batches * (r.setups * get("setup_time") * rate + fee) + q * per_part * rate


def _payback_at(r: Route, current: Route, point: dict, costs: ShopCosts) -> int | None:
    """The fewest parts at which buying costs no more than the current plan, at one point; None past the cap."""
    for q in range(1, PAYBACK_CAP + 1):
        if _route_cost(r, point, q, costs) <= _route_cost(current, point, q, costs) + 1e-9:
            return q
    return None


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
