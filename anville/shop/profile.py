"""The shop profile: the planner's ShopProfile plus what running the shop costs (RFC 0002 §4).

Every cost input is a range with its unit, status and source. A number Rayyan hasn't supplied is a
`placeholder`: it is labelled as one wherever it is shown, so a review can say so and nobody reads it
as a measurement. To replace one, change its range, status and source here.
"""

from dataclasses import dataclass, field

from anville.geometry.shop import ShopProfile

STATUSES = ("verified", "sourced", "stated", "placeholder")


@dataclass(frozen=True)
class Input:
    lo: float
    hi: float
    unit: str    # $ | $/hr | hr
    status: str  # verified (checked this week) | sourced (a published range) | stated (Rayyan) | placeholder
    source: str

    def __post_init__(self):
        if self.lo > self.hi:
            raise ValueError(f"input range {self.lo}..{self.hi} is reversed")
        if self.status not in STATUSES:
            raise ValueError(f"input status {self.status!r} is not one of {STATUSES}")


@dataclass(frozen=True)
class Scenarios:
    """Quantities are fixed scenarios, never a range: batches(q) steps, so it can't sit inside the corner box."""

    quantities: tuple[int, ...]
    batch_size: int  # parts per setup of the job
    status: str
    source: str
    # Part designs a bought tool serves, this one included, each at the same quantity: a tool's price is
    # divided among them (RFC 0002 §4). Soft jaws are not: they are re-cut for each stock size (§3).
    tool_parts: int = 1


@dataclass(frozen=True)
class ShopCosts:
    mill_rate: Input          # $/hr, the in-house 3-axis mill, operator included
    setup_time: Input         # hr per setup, once per batch
    load_time: Input          # hr per part per setup
    cycle_time: Input         # hr per part: all cutting, the same on every route
    back_chamfer_pass: Input  # hr per part per chamfer finished with a back-chamfer cutter, over a standard pass
    jaw_blank: Input          # $ per pair of soft-jaw blanks
    jaw_machining: Input      # hr on the mill to cut the reliefs into the jaws
    long_reach_price: Input   # $ for an extended-reach endmill (an add_tool option outside the catalog)
    outsource_rate: Input     # $/hr at an outside 5-axis shop
    outsource_fee: Input      # $ per batch sent out: shipping, handling, order minimum
    scenarios: Scenarios
    tool_price: dict[str, Input] = field(default_factory=dict)  # $ for a catalog tool, by name


@dataclass(frozen=True, kw_only=True)
class Shop(ShopProfile):
    """A shop the planner can plan for and the cost model can price."""

    name: str
    costs: ShopCosts


_TODO = "placeholder: not yet supplied by Rayyan (RFC 0002 open question {})"
_RATES = "2026 vendor pricing guides (fabcon, rapiddirect, jiga), RFC 0002: most quotes $60-100"


def _placeholder(lo, hi, unit, question=2):
    return Input(lo, hi, unit, "placeholder", _TODO.format(question))


# The demo shop: Marquette's DLL (RFC 0002). Two numbers are known; the rest are labelled placeholders.
DLL_COSTS = ShopCosts(
    mill_rate=Input(40.0, 120.0, "$/hr", "sourced", _RATES),
    setup_time=_placeholder(0.5, 1.5, "hr"),
    load_time=_placeholder(2 / 60, 6 / 60, "hr"),
    cycle_time=_placeholder(0.25, 0.75, "hr"),
    back_chamfer_pass=_placeholder(1 / 60, 4 / 60, "hr"),
    jaw_blank=_placeholder(20.0, 60.0, "$"),
    jaw_machining=_placeholder(0.5, 1.0, "hr"),  # RFC 0002 §3 says "about 30 min": an estimate, not a source
    long_reach_price=_placeholder(40.0, 150.0, "$"),
    outsource_rate=_placeholder(75.0, 200.0, "$/hr"),
    outsource_fee=_placeholder(50.0, 150.0, "$"),
    scenarios=Scenarios((4, 10, 20), 10, "placeholder",
                        _TODO.format(3) + "; tool_parts = 2 stated by Rayyan, 2026-10-29: "
                                          "the cutter's price is spread over Parts A and B", tool_parts=2),
    tool_price={"910480": Input(243.50, 243.50, "$", "verified", "harveytool.com, checked 2026-10-28 (RFC 0002)")},
)
