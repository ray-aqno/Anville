"""Geometry layer: STEP -> PartFacts -> SetupPlan (ADR 0008).

`extract` needs OCP; everything else is pure Python, so it is imported lazily.
"""

from anville.geometry.facts import DIRECTIONS, Access, Edge, EdgeChamfer, FaceFacts, PartFacts, face_groups
from anville.geometry.edits import EvaluateResult, Option, Skipped, evaluate_edits
from anville.geometry.plan import Assignment, Setup, SetupPlan, Unreachable, min_setups
from anville.geometry.shop import (
    BACK_CHAMFER_CATALOG, HARVEY_910480, BackChamferTool, ShopProfile, SoftJaws, Tool,
)


def extract(step_path):
    from anville.geometry.step import extract as _extract

    return _extract(step_path)


def assess_back_chamfer(step_path, facts, shop):
    from anville.geometry.backchamfer import assess_back_chamfer as _assess

    return _assess(step_path, facts, shop)


__all__ = ["BACK_CHAMFER_CATALOG", "DIRECTIONS", "HARVEY_910480", "Access", "Assignment", "BackChamferTool", "Edge", "EdgeChamfer",
           "EvaluateResult", "FaceFacts", "Option", "PartFacts", "Setup", "SetupPlan", "ShopProfile", "Skipped",
           "SoftJaws", "Tool", "Unreachable", "assess_back_chamfer", "evaluate_edits", "extract", "face_groups",
           "min_setups"]
