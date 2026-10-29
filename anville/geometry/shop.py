"""The subset of a shop profile the setup planner reads.

The full shop-profile format is an open question (CLAUDE.md §4); it can supersede this.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tool:
    name: str
    diameter: float  # mm
    max_ld: float    # usable reach as a multiple of diameter

    @property
    def reach(self) -> float:
        return self.diameter * self.max_ld


@dataclass(frozen=True)
class BackChamferTool:
    """A back-chamfer cutter (RFC 0002): a cone that flares from the neck down to the head and cuts upward."""

    name: str
    angle: float          # degrees, chamfer angle it cuts
    head_diameter: float  # mm
    neck_diameter: float  # mm
    neck_length: float    # mm, usable length below the shank
    length_of_cut: float  # mm, the largest leg it cuts
    head_flat: float = 1.0  # mm of cylindrical head below the cone: assumed, not in the catalog


# Harvey Tool 910480, checked on harveytool.com 2026-10-28 (RFC 0002): 0.500" head, 0.317" neck,
# 2.5" neck length, 0.088" length of cut, 45°, $243.50.
HARVEY_910480 = BackChamferTool("910480", 45.0, 12.70, 8.05, 63.5, 2.24)

# Cutters the shop could buy. evaluate_edits offers one only after it passes checks a-f on the part.
BACK_CHAMFER_CATALOG = (HARVEY_910480,)


@dataclass(frozen=True)
class SoftJaws:
    """RFC 0002 §3: a 6" vise with soft jaws on the faces across the chamfered edge, the part on parallels.

    The sizes are assumptions, stated on the setup sheet; only the relief is derived (from the swept cutter).
    """

    relieved: bool = True
    parallel_setback: float | None = None  # mm in from the chamfered side; None = head radius + 1 (RFC)
    parallel_height: float = 12.7          # mm (1/2")
    parallel_width: float = 3.2            # mm (1/8")
    jaw_thickness: float = 20.0            # mm, outboard of the part
    jaw_width: float = 152.4               # mm (6"), centred on the part
    overtravel: float | None = None        # mm past each end of the edge; None = head radius
    relief_margin: float = 1.0             # mm around the swept cutter
    # Share of each jaw's grip face the reliefs must leave. 50% is a conservative bar, not a measured one:
    # soft jaws hold by friction over their contact patch, and Part A keeps 96%. Confirm with the machinist.
    min_grip: float = 0.5


@dataclass(frozen=True)
class ShopProfile:
    tools: list[Tool]
    back_chamfer_tools: list[BackChamferTool] = field(default_factory=list)
    fixture: SoftJaws | None = None
