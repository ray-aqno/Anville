"""Shared agent fixtures: demo Part A, the default shop (relieved soft jaws, no back-chamfer cutter),
Part A's bottom chamfer group, and the catalog cutter's assessment on Part A."""

from dataclasses import replace

import pytest

from anville.agent.review import find_issues
from anville.geometry import (
    BACK_CHAMFER_CATALOG, ShopProfile, SoftJaws, Tool, assess_back_chamfer, extract, min_setups,
)


@pytest.fixture(scope="module")
def shop():
    return ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)],
                       fixture=SoftJaws())


@pytest.fixture(scope="module")
def part_a_step(tmp_path_factory):
    from tests.geometry.parts import PARTS, write_step  # OCP

    return write_step(PARTS["demo_bracket"](), tmp_path_factory.mktemp("agent") / "a.step")


@pytest.fixture(scope="module")
def part_a(part_a_step):
    return extract(part_a_step)


@pytest.fixture(scope="module")
def catalog_access(part_a_step, part_a, shop):
    """What analyze() runs: checks a-f for the shop's cutters plus the catalog's, on Part A."""
    return assess_back_chamfer(part_a_step, part_a, replace(shop, back_chamfer_tools=list(BACK_CHAMFER_CATALOG)))


@pytest.fixture(scope="module")
def chamfer(part_a, shop):
    (issue,) = find_issues(part_a, min_setups(part_a, shop))
    return issue.face_group
