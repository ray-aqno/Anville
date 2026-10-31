"""anchor(): code picks and paints the flagged group; SAM 3.1 traces it on a plain render, and its outline is drawn
only when it agrees with the code (RFC 0003: SAM never chooses the face). SAM is faked here except for the decode
test, which reads a real sam-3.1 reply to a synthetic render."""

import json
import struct
import zlib
from pathlib import Path

import pytest

from anville.geometry import sam_anchor as anchor_mod
from anville.geometry.sam_anchor import (AGREE, OUTLINE, SamClient, SamUnavailable, anchor, mask_pixels,
                                     request_body)
from anville.geometry.render import HIGHLIGHT

DATA = Path(__file__).parent / "data"


def _rgb(png: bytes):
    """Pixels of a render.py PNG (one IDAT run, RGB8, filter 0 rows): (w, h, rows of (r, g, b))."""
    w, h = struct.unpack(">II", png[16:24])
    i, idat = 8, b""
    while i < len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        if png[i + 4:i + 8] == b"IDAT":
            idat += png[i + 8:i + 8 + n]
        i += 12 + n
    raw, stride = zlib.decompress(idat), 1 + 3 * w
    return w, h, [[tuple(raw[y * stride + 1 + 3 * x:y * stride + 4 + 3 * x]) for x in range(w)] for y in range(h)]


def _orange(png: bytes) -> set:
    """The code's pixels, by render.py's own keying rule (R > B), not by anchor's."""
    _, _, px = _rgb(png)
    return {(x, y) for y, row in enumerate(px) for x, p in enumerate(row) if p[0] > p[2]}


class FakeSam:
    model, host = "sam-3.1", "api.meta.ai"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def segment(self, png, size, box, point):
        self.calls.append({"png": png, "size": size, "box": box, "point": point})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(png, size, box, point)


@pytest.fixture(scope="module")
def bracket(step, facts):
    return step("demo_bracket"), facts("demo_bracket")


@pytest.fixture(scope="module")
def code_g1(bracket):
    """G1's pixels as the code paints them: render.py's highlighted image, read by colour."""
    from anville.geometry.render import render
    path, f = bracket
    return _orange(render(path, f.group_faces("G1")).png)


def test_sam_sees_a_plain_render_with_the_groups_box_and_a_point_on_it(bracket, code_g1):
    sam = FakeSam(lambda *a: frozenset(code_g1))
    anchor(*bracket, "G1", sam=sam)
    (call,) = sam.calls
    assert not _orange(call["png"])  # no orange for SAM to key on: it traces the face, not our paint
    x0, y0, x1, y1 = call["box"]
    assert (min(x for x, _ in code_g1), min(y for _, y in code_g1)) == (x0, y0)
    assert (max(x for x, _ in code_g1) + 1, max(y for _, y in code_g1) + 1) == (x1, y1)  # exclusive, like render
    assert call["point"] in code_g1


def test_an_agreeing_mask_is_traced_and_recorded(bracket, code_g1):
    a = anchor(*bracket, "G1", sam=FakeSam(lambda *a: frozenset(code_g1)))
    assert a.record == {"group": "G1", "model": "sam-3.1", "host": "api.meta.ai",
                        "latency_s": a.record["latency_s"], "iou": 1.0, "agreed": True, "error": None}
    _, _, px = _rgb(a.png)
    assert sum(p == OUTLINE for row in px for p in row) > 100  # SAM's outline is on the picture


def test_a_disagreeing_mask_is_not_drawn(bracket, code_g1):
    elsewhere = frozenset((x + 120, y) for x, y in code_g1)
    a = anchor(*bracket, "G1", sam=FakeSam(lambda *a: elsewhere))
    assert a.record["agreed"] is False and a.record["iou"] < AGREE
    _, _, px = _rgb(a.png)
    assert not any(p == OUTLINE for row in px for p in row)
    assert any(p == HIGHLIGHT or (p[0] > p[2]) for row in px for p in row)  # the code's fill still shows


def test_sam_failing_still_gives_the_codes_picture(bracket):
    a = anchor(*bracket, "G1", sam=FakeSam(SamUnavailable("HTTP 503: busy")))
    assert a.record["agreed"] is None and a.record["iou"] is None
    assert a.record["error"] == "HTTP 503: busy"
    assert _orange(a.png)


def test_no_key_means_no_sam_and_no_call(bracket):
    assert SamClient.from_env({}) is None
    a = anchor(*bracket, "G1", sam=None)
    assert a.record["agreed"] is None and a.record["error"] == "SAM not configured (no MODEL_API_KEY)"
    assert a.record["model"] == "sam-3.1" and a.record["latency_s"] is None


def test_the_label_chip_sits_by_the_group(bracket, code_g1):
    a = anchor(*bracket, "G1", sam=None)
    _, _, px = _rgb(a.png)
    chip = {(x, y) for y, row in enumerate(px) for x, p in enumerate(row) if p == anchor_mod.CHIP}
    assert len(chip) > 200
    cx = sum(x for x, _ in chip) / len(chip)
    assert min(x for x, _ in code_g1) - 80 < cx < max(x for x, _ in code_g1) + 80


def test_a_diagonal_stripes_label_sits_on_the_stripe_not_its_box_corner(bracket):
    from anville.geometry.render import render
    path, f = bracket
    g3 = _orange(render(path, f.group_faces("G3")).png)  # the bottom chamfer: a long diagonal stripe
    a = anchor(path, f, "G3", sam=None)
    _, _, px = _rgb(a.png)
    chip = {(x, y) for y, row in enumerate(px) for x, p in enumerate(row) if p == anchor_mod.CHIP}
    bottom = max(y for _, y in chip)
    under = [y for x, y in g3 if min(cx for cx, _ in chip) <= x <= max(cx for cx, _ in chip) and y > bottom]
    assert under and min(under) - bottom <= 20  # the stripe starts just below the chip


def test_a_group_no_view_shows_gives_none(bracket, monkeypatch):
    real = anchor_mod.render

    def hidden(*a, **k):
        r = real(*a, **k)
        r.pieces, r.highlighted, r.box = [], 0, None
        return r

    monkeypatch.setattr(anchor_mod, "render", hidden)
    sam = FakeSam(lambda *a: frozenset())
    assert anchor(*bracket, "G1", sam=sam) is None and sam.calls == []


def test_a_real_sam_reply_decodes_to_its_pixels():
    reply = json.loads((DATA / "sam_reply_demo_bracket_g1.synthetic.json").read_text())
    px = mask_pixels(reply["text"])
    assert len(px) == 25975  # decoded once by hand with meta-sam-parser, 2026-10-31
    assert min(x for x, _ in px) >= 488 and min(y for _, y in px) >= 301
    assert max(x for x, _ in px) <= 748 and max(y for _, y in px) <= 549


def test_a_reply_without_a_mask_is_unavailable():
    with pytest.raises(SamUnavailable, match="no mask"):
        mask_pixels("<0f>")


def test_the_request_is_the_shape_the_live_api_took():
    body = request_body(b"\x89PNG", (800, 600), (488, 301, 749, 551), (600, 420))
    assert body["model"] == "sam-3.1"
    (msg,) = body["input"]
    text, image = msg["content"]
    assert text == {"type": "input_text",
                    "text": "<0f>1<|box;x1=488;y1=301;x2=748;y2=550;w=800;h=600|><|point;x=600;y=420;w=800;h=600|>"}
    assert image == {"type": "input_image", "image_url": "data:image/png;base64,iVBORw=="}


def test_the_client_posts_with_the_key_and_reads_the_text():
    seen = {}

    def post(url, body, headers, timeout_s):
        seen.update(url=url, body=body, headers=headers)
        return json.dumps({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.loads((DATA / "sam_reply_demo_bracket_g1.synthetic.json")
                                                       .read_text())["text"]}]}]}).encode()

    sam = SamClient("k-test", post=post)
    px = sam.segment(b"\x89PNG", (800, 600), (488, 301, 749, 551), (600, 420))
    assert len(px) == 25975
    assert seen["url"] == "https://api.meta.ai/v1/responses"
    assert seen["headers"]["Authorization"] == "Bearer k-test"
    assert json.loads(seen["body"])["model"] == "sam-3.1"


def test_a_mask_smaller_than_its_box_covers_the_whole_box():
    # Codex P2: a half-resolution mask mapped cell-to-pixel covered a quarter of its box and faked a disagreement.
    from anville.geometry.sam_anchor import _to_image
    px = _to_image(b"\x01\x01\x01\x00", 2, 2, left=10, top=20, bw=4, bh=4)
    assert px == {(x, y) for x in range(10, 14) for y in range(20, 24)} - {(x, y) for x in (12, 13) for y in (22, 23)}


@pytest.mark.parametrize("reply", [{"output": [None]}, {"output": [{"type": "message", "content": [None]}]}, [1],
                                   {"status": "completed"}])
def test_a_malformed_reply_is_a_failed_sam_call(reply):
    # Codex P2: a null entry raised AttributeError out of anchor() instead of costing only the outline.
    sam = SamClient("k", post=lambda *a: json.dumps(reply).encode())
    with pytest.raises(SamUnavailable, match="malformed"):
        sam.segment(b"\x89PNG", (800, 600), (0, 0, 10, 10), (5, 5))
