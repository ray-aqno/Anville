"""Geometry layer: STEP -> PartFacts -> SetupPlan (ADR 0008).

`extract` needs OCP; everything else is pure Python, so it is imported lazily.
"""

from anville.geometry.facts import DIRECTIONS, Access, Edge, FaceFacts, PartFacts, face_groups
from anville.geometry.plan import Assignment, Setup, SetupPlan, Unreachable, min_setups
from anville.geometry.shop import ShopProfile, Tool


def extract(step_path):
    from anville.geometry.step import extract as _extract

    return _extract(step_path)


__all__ = ["DIRECTIONS", "Access", "Assignment", "Edge", "FaceFacts", "PartFacts", "Setup", "SetupPlan",
           "ShopProfile", "Tool", "Unreachable", "extract", "face_groups", "min_setups"]
