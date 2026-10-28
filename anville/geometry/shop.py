"""The subset of a shop profile the setup planner reads.

The full shop-profile format is an open question (CLAUDE.md §4); it can supersede this.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    diameter: float  # mm
    max_ld: float    # usable reach as a multiple of diameter

    @property
    def reach(self) -> float:
        return self.diameter * self.max_ld


@dataclass(frozen=True)
class ShopProfile:
    tools: list[Tool]
