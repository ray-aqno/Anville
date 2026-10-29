"""A real Onshape export (Rayyan, 2026-10-29): a Z bracket of 1/2" plate, 7" × 3" × 2.41".

The web carries a Ø1.75" bore and two 3/8" holes; each flange carries two side holes. It is the first
part not built in code: AP242 from ST-Developer, in metres, with inch-fraction radii.
"""

from pathlib import Path

import pytest

from anville.geometry import extract, min_setups

STEP = Path(__file__).parent / "data" / "onshape_z_bracket.step"
WEB_HOLES = {11, 13}  # the two 3/8" holes through the 12.7 mm web


@pytest.fixture(scope="module")
def bracket():
    return extract(STEP)


def test_metres_are_read_as_millimetres(bracket):
    lo, hi = bracket.bbox
    assert [h - l for l, h in zip(lo, hi)] == pytest.approx([7 * 25.4, 3 * 25.4, 61.239], abs=1e-3)


def test_faces_and_holes(bracket):
    assert len(bracket.faces) == 17 and bracket.edge_chamfers == []
    radii = sorted(f.radius for f in bracket.faces if f.surface == "cylinder")
    assert radii == pytest.approx([4.7625, 4.7625, 6.3179, 6.35, 6.35, 6.6921, 22.225], abs=1e-4)
    # The Z bend's two inside corners are sharp and run along Y: they limit nothing cut from ±Z.
    assert sorted((e.kind, e.axis, e.radius) for e in bracket.edges if e.kind == "concave") \
        == [("concave", "Y", None)] * 2


def test_setups(bracket, shop):
    three = min_setups(bracket, shop)
    assert sorted(s.directions[0] for s in three.setups) == ["+X", "+Z", "-X", "-Z"]
    assert min_setups(bracket, shop, axes=5).count == 1


@pytest.mark.xfail(strict=True, reason="ADR 0008 §4 measures reach from the stock top, the -X flange's top; "
                                        "a holder beside the flange needs only the web's 12.7 mm (Amendment 1)")
def test_web_holes_are_reachable(bracket, shop):
    assert not WEB_HOLES & {u.face_id for u in min_setups(bracket, shop).unreachable}
