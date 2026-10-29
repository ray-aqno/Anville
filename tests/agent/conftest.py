"""Shared agent fixtures: demo Part A, the default shop, and Part A's bottom chamfer group."""

import pytest

from anville.agent.review import find_issues
from anville.geometry import ShopProfile, Tool, extract, min_setups


@pytest.fixture(scope="module")
def shop():
    return ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)])


@pytest.fixture(scope="module")
def part_a(tmp_path_factory):
    from tests.geometry.parts import PARTS, write_step  # OCP

    return extract(write_step(PARTS["demo_bracket"](), tmp_path_factory.mktemp("agent") / "a.step"))


@pytest.fixture(scope="module")
def chamfer(part_a, shop):
    (issue,) = find_issues(part_a, min_setups(part_a, shop))
    return issue.face_group
