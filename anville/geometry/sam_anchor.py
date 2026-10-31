"""The flagged group, shown so a judge can see which face Anville means (locked decision 5, RFC 0003/0004).

Code picks the group and paints it (render.py: exact, from the B-rep). SAM 3.1 gets a plain render from the same
camera, with a box and a point on the group, and traces what it sees. SAM never chooses the face: its outline is
drawn only when its mask agrees with the code's pixels (IoU ≥ AGREE); otherwise the picture is the code's alone and
the record says SAM disagreed. A SAM failure costs only the outline, never the picture.

sam-3.1 runs on the Meta Model API (ADR 0004). Verified 2026-10-31 with one call: POST /v1/responses, stream off,
the image as a base64 data: URL, HTTP 200 in 0.57 s; the mask payload is decoded with Meta's meta-sam-parser.
"""

from __future__ import annotations

import base64
import zlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

import meta_sam_parser as msp

from anville.geometry.facts import PartFacts
from anville.geometry.render import _png, render

SAM_URL = "https://api.meta.ai/v1/responses"
MODEL, HOST = "sam-3.1", "api.meta.ai"
AGREE = 0.8                 # SAM's mask vs the code's pixels: at or above this, its outline is drawn
OUTLINE = (8, 102, 255)     # SAM's trace. B > R, so render.py's R > B key never mistakes it for the highlight
CHIP = (20, 24, 32)         # the label's ground: darker than any part shade or edge render.py draws
TEXT = (255, 255, 255)
SCALE, PAD = 3, 6           # label glyphs are 5×7 cells drawn SCALE pixels wide, PAD pixels of chip around them

# 5×7 glyphs for group labels (G1, G11, ...).
GLYPHS = {
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}

Pixels = frozenset[tuple[int, int]]
Box = tuple[int, int, int, int]  # x0, y0, x1, y1, right and bottom exclusive (render.py's convention)


class SamUnavailable(Exception):
    """SAM refused, failed or answered without a usable mask. The picture goes out without its outline."""


class Segmenter(Protocol):
    model: str
    host: str

    def segment(self, png: bytes, size: tuple[int, int], box: Box, point: tuple[int, int]) -> Pixels: ...


@dataclass
class Anchor:
    png: bytes
    record: dict  # for the trace: group, model, host, latency_s, iou, agreed, error


def request_body(png: bytes, size: tuple[int, int], box: Box, point: tuple[int, int]) -> dict:
    """One object, prompted by the group's box and a point on it, in the image's own pixels."""
    prompt = msp.build_object_prompt(size=size, objects=[msp.PromptObject(id=1, prompts=[msp.FramePrompt(
        box=msp.PromptBox(left=box[0], top=box[1], right=box[2], bottom=box[3]),
        points=[msp.PromptPoint(x=point[0], y=point[1])])])])
    return {"model": MODEL, "input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": prompt},
        {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png).decode()}]}]}


def mask_pixels(text: str) -> Pixels:
    """The first mask in a sam-3.1 reply, as image pixels. The mask raster covers its bounds (the box)."""
    parser = msp.image_segmentation_format().create_parser()
    try:
        parser.push(text)
        records = parser.finish(msp.CompletedOutcome()).result.records
        mask = next((r for r in records if r.kind == "mask"), None)
        if mask is None:
            raise SamUnavailable(f"no mask in SAM's reply: {text[:120]!r}")
        raster = msp.decode_mask_to_raster(mask.mask)
    except SamUnavailable:
        raise
    except Exception as e:  # the parser's own errors: a reply we can't read is no answer
        raise SamUnavailable(f"unreadable SAM reply ({type(e).__name__}): {e}"[:200]) from None
    b = mask.bounds
    return _to_image(raster, mask.mask.width, mask.mask.height, left=int(b.left), top=int(b.top),
                     bw=int(b.right - b.left), bh=int(b.bottom - b.top))


def _to_image(raster: bytes, w: int, h: int, *, left: int, top: int, bw: int, bh: int) -> Pixels:
    """The image pixels a w×h raster covers when stretched over its bw×bh bounds: each image pixel takes the cell
    it falls in, so a coarser mask still covers its whole box (Codex P2: cell-to-pixel covered a quarter)."""
    return frozenset((left + x, top + y) for y in range(bh) for x in range(bw)
                     if raster[(y * h // bh) * w + x * w // bw])


def _urllib_post(url: str, body: bytes, headers: dict, timeout_s: float) -> bytes:
    """POST within a budget for the whole request (agent.model's trickle-proof send)."""
    import urllib.request

    from anville.agent.model import ModelUnavailable, OpenAICompatModel

    try:
        return OpenAICompatModel._send(urllib.request.Request(url, data=body, method="POST", headers=headers),
                                       timeout_s)
    except ModelUnavailable as e:
        raise SamUnavailable(str(e)) from None


class SamClient:
    model, host = MODEL, HOST

    def __init__(self, api_key: str, *, url: str = SAM_URL, timeout_s: float = 30.0,
                 post: Callable[[str, bytes, dict, float], bytes] = _urllib_post):
        self._key, self._url, self._timeout, self._post = api_key, url, timeout_s, post

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SamClient | None":
        key = env.get("MODEL_API_KEY")
        return cls(key) if key else None

    def segment(self, png: bytes, size: tuple[int, int], box: Box, point: tuple[int, int]) -> Pixels:
        raw = self._post(self._url, json.dumps(request_body(png, size, box, point)).encode(),
                         {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                         self._timeout)
        try:
            reply = json.loads(raw)
            text = next(c["text"] for o in reply["output"] if o.get("type") == "message"
                        for c in o["content"] if c.get("type") == "output_text")
            status, error = reply.get("status"), reply.get("error")
        except (ValueError, KeyError, TypeError, AttributeError, StopIteration):  # AttributeError: a null entry
            raise SamUnavailable(f"malformed SAM response: {raw[:120]!r}") from None
        if status != "completed":
            raise SamUnavailable(f"SAM response {status!r}: {error}")
        return mask_pixels(text)


def _rows(png: bytes) -> tuple[int, int, bytearray]:
    """RGB bytes of a render.py PNG (its own output: RGB8, every row filter 0)."""
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    i, idat = 8, b""
    while i < len(png):
        n = int.from_bytes(png[i:i + 4], "big")
        if png[i + 4:i + 8] == b"IDAT":
            idat += png[i + 8:i + 8 + n]
        i += 12 + n
    raw, stride = zlib.decompress(idat), 1 + 3 * w
    return w, h, bytearray(b"".join(raw[y * stride + 1:(y + 1) * stride] for y in range(h)))


def _paint(rgb: bytearray, w: int, h: int, x: int, y: int, colour) -> None:
    if 0 <= x < w and 0 <= y < h:
        rgb[3 * (y * w + x):3 * (y * w + x) + 3] = bytes(colour)


def _outline(rgb: bytearray, w: int, h: int, mask: Pixels) -> None:
    """The mask's border, two pixels wide, so it reads at panel size."""
    edge = [(x, y) for x, y in mask if any(q not in mask for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))]
    for x, y in edge:
        for dx in (-1, 0):
            for dy in (-1, 0):
                _paint(rgb, w, h, x + dx, y + dy, OUTLINE)


def _label(rgb: bytearray, w: int, h: int, text: str, code: Pixels, at: tuple[int, int]) -> None:
    """A dark chip with the group's name, centred over `at` (the point SAM was given) and just above the group's
    topmost pixel in that column, below the group when there's no room above. Placed by the group's box corner, a
    long diagonal stripe got its label far off the stripe."""
    cw = PAD * 2 + len(text) * 6 * SCALE - SCALE
    ch = PAD * 2 + 7 * SCALE
    column = [y for x, y in code if abs(x - at[0]) <= cw // 2]
    top, bottom = min(column), max(column)
    x = min(max(at[0] - cw // 2, 0), w - cw)
    y = top - ch - 6 if top - ch - 6 >= 0 else min(bottom + 6, h - ch)
    for yy in range(y, y + ch):
        for xx in range(x, x + cw):
            _paint(rgb, w, h, xx, yy, CHIP)
    for i, c in enumerate(text):
        for row, bits in enumerate(GLYPHS[c]):
            for col, bit in enumerate(bits):
                if bit == "1":
                    for sy in range(SCALE):
                        for sx in range(SCALE):
                            _paint(rgb, w, h, x + PAD + (i * 6 + col) * SCALE + sx, y + PAD + row * SCALE + sy, TEXT)


def anchor(step_path: str | Path, facts: PartFacts, group: str, *, sam: Segmenter | None,
           clock: Callable[[], float] = time.monotonic) -> Anchor | None:
    """The picture of `group` (G1, G11, ...) for the panel and the setup sheet, and its trace record. None when no
    view shows the group. `sam` is `SamClient.from_env(env)`; None skips SAM (the record says so)."""
    painted = render(step_path, facts.group_faces(group))
    if not painted.pieces:
        return None
    plain = render(step_path, [], view=painted.view)  # same part, same view: same camera, no paint
    w, h, rgb = _rows(painted.png)
    box = painted.pieces[0][0]
    code = frozenset((x, y) for y in range(box[1], box[3]) for x in range(box[0], box[2])
                     if rgb[3 * (y * w + x)] > rgb[3 * (y * w + x) + 2])  # render.py's key: R > B
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    point = min(code, key=lambda p: ((p[0] - cx) ** 2 + (p[1] - cy) ** 2, p))

    record = {"group": group, "model": getattr(sam, "model", MODEL), "host": getattr(sam, "host", HOST),
              "latency_s": None, "iou": None, "agreed": None, "error": None}
    if sam is None:
        record["error"] = "SAM not configured (no MODEL_API_KEY)"
    else:
        start = clock()
        try:
            traced = sam.segment(plain.png, (w, h), box, point)
        except SamUnavailable as e:
            record["error"] = str(e)[:300]
        else:
            iou = len(traced & code) / len(traced | code) if traced | code else 0.0
            record.update(iou=round(iou, 3), agreed=iou >= AGREE)
            if record["agreed"]:
                _outline(rgb, w, h, traced)
        record["latency_s"] = round(clock() - start, 2)
    _label(rgb, w, h, group, code, point)
    return Anchor(_png(w, h, rgb), record)
