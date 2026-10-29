import pytest

from anville.geometry import ShopProfile, Tool, extract


@pytest.fixture(scope="session")
def shop():
    """A small shop: three flat endmills at a conservative 4×D reach."""
    return ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)])


@pytest.fixture(scope="session")
def step(tmp_path_factory):
    """Write each synthetic part as STEP once per session: name -> path."""
    from tests.geometry.parts import PARTS, write_step  # OCP: keep it out of test_plan.py's collection

    out = tmp_path_factory.mktemp("parts")
    cache = {}

    def get(name):
        if name not in cache:
            cache[name] = write_step(PARTS[name](), out / f"{name}.step")
        return cache[name]

    return get


@pytest.fixture(scope="session")
def facts(step):
    """Extract each synthetic part once per session: name -> PartFacts."""
    cache = {}

    def get(name):
        if name not in cache:
            cache[name] = extract(step(name))
        return cache[name]

    return get
