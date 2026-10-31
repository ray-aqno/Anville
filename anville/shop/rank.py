"""rank_fixes (ADR 0010): code ranks the any-intent fixes by cost, as far as the data settles it.

Fix A outranks fix B only if A costs strictly less at every corner of the input box, at the stated quantity
or else at every scenario quantity, and every cost A leaves unpriced B leaves unpriced too: an unpriced cost
can only make a route dearer. The top tier is every complete fix that no other outranks, in the caller's
order (best_option's). In a tie, the fix strictly cheapest with every ranged input at its midpoint, at every
one of those quantities, leads, if it leaves nothing unpriced that the others price (Rayyan, 2026-10-30).
Otherwise no fix leads and the model chooses among the tier.
"""

from dataclasses import dataclass

from anville.shop.cost import TOL, CostResult, Route, _at, _corners, _ranged, _terms
from anville.shop.profile import Shop


@dataclass(frozen=True)
class Ranking:
    top: list[str]    # the top tier: no other fix outranks these; in the caller's order
    lead: str | None  # the fix code puts first; None when the model chooses among `top`
    basis: str        # dominates | mid_range (the tie-break) | tie | sole (one complete fix) | none


def rank_fixes(result: CostResult, shop: Shop, keys: list[str], *, quantity: int | None = None,
               waived: frozenset[str] | set[str] = frozenset()) -> Ranking:
    """Rank the fixes `keys` (plan-changing; best_option order) priced in `result`.

    `quantity`: the designer's stated quantity; without one, a ranking must hold at every scenario quantity.
    `waived`: unpriced costs that no longer count against a fix: the designer's own edit time, once they've said
    the face may change (ADR 0010 Amendment 1).
    """
    routes = {r.key: r for r in result.routes}
    missing = [k for k in keys if k not in routes]
    if missing:
        raise ValueError(f"not priced in this cost result: {missing}")
    fixes = [routes[k] for k in keys if routes[k].complete]  # a fix that leaves faces unreachable never ranks
    if len(fixes) < 2:
        return Ranking([r.key for r in fixes], fixes[0].key if fixes else None, "sole" if fixes else "none")

    costs = shop.costs
    quantities = (quantity,) if quantity is not None else tuple(costs.scenarios.quantities)
    if not quantities:
        raise ValueError("no quantity to rank at: state one or give the shop's scenarios some")
    ranged = _ranged(costs)
    n = costs.scenarios.batch_size

    def dollars(r: Route, point: dict) -> list[float]:
        terms = _terms(r, point, costs)
        return [_at(terms, q, n) for q in quantities]

    corners = list(_corners(ranged))
    at = {r.key: [x for c in corners for x in dollars(r, c)] for r in fixes}

    def cheaper(a: Route, b: Route, table: dict) -> bool:
        return set(a.unpriced) - set(waived) <= set(b.unpriced) and all(x < y - TOL for x, y in zip(table[a.key], table[b.key]))

    top = [b for b in fixes if not any(cheaper(a, b, at) for a in fixes if a is not b)]
    if len(top) == 1:
        return Ranking([top[0].key], top[0].key, "dominates")
    mid = {r.key: dollars(r, {name: (i.lo + i.hi) / 2 for name, i in ranged.items()}) for r in top}
    lead = next((a for a in top if all(cheaper(a, b, mid) for b in top if b is not a)), None)
    return Ranking([r.key for r in top], lead.key if lead else None, "mid_range" if lead else "tie")
