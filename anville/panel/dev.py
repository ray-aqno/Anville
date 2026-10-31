"""Run the panel locally against a trace fixture, dripped out at its recorded pace (no store, no Onshape).

    uv run --with uvicorn python -m anville.panel.dev [fixture.json] [--speed 1.0] [--render face.png]

Each event becomes visible once its recorded offset has passed (gaps longer than MAX_GAP_S are shortened,
so a fixture's older runs don't hold up the newest). Serves on 127.0.0.1:8765 with token "dev". It also serves
the traveler for the sheet fixtures, and prints each shop note it would have recorded.
"""

import argparse
import json
import time
from pathlib import Path

from fastapi import FastAPI

from anville.panel import make_router, make_sheet_router, sheet_sig

FIXTURES = Path(__file__).resolve().parents[2] / "tests/panel/fixtures"
DEFAULT = FIXTURES / "part_a_live.synthetic.json"
MAX_GAP_S = 3.0
TOKEN = "dev"


def dripping_reader(events: list[dict], speed: float):
    offsets, at = [], 0.0
    for prev, e in zip([None, *events], events):
        at += 0.0 if prev is None else min(e["ts"] - prev["ts"], MAX_GAP_S) / speed
        offsets.append(at)
    start = time.monotonic()

    def read(document_id: str, after: int, limit: int) -> list[dict]:
        now = time.monotonic() - start
        return [e for e, off in zip(events, offsets)
                if e["document_id"] == document_id and e["cursor"] > after and off <= now][:limit]
    return read


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", nargs="?", default=str(DEFAULT))
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--render", help="a PNG to serve as every version's flagged-face picture")
    args = ap.parse_args()
    data = json.loads(Path(args.fixture).read_text())
    png = Path(args.render).read_bytes() if args.render else None
    read_render = (lambda _version_id: png) if png else None
    app = FastAPI()
    app.include_router(make_router(dripping_reader(data["events"], args.speed), lambda *a, **k: True,
                                   panel_secret=TOKEN, allowed_docs=lambda: {data["document_id"]},
                                   read_render=read_render))
    sheets = {d["sheet_id"]: (d["document_id"], d["sheet"])
              for d in (json.loads(p.read_text()) for p in sorted(FIXTURES.glob("sheet_*.synthetic.json")))}

    def note(key, document_id, payload, **_):
        print("shop note:", key, json.dumps(payload), flush=True)
        return True

    # The fixtures predate version_id on stored sheets: give them one, so --render shows on the sheet too.
    sheets = {sid: (doc, {"version_id": "v-dev", **sheet}) for sid, (doc, sheet) in sheets.items()}
    app.include_router(make_sheet_router(sheets.get, note, panel_secret=TOKEN, read_render=read_render))
    print(f"http://127.0.0.1:{args.port}/panel?documentId={data['document_id']}&t={TOKEN}", flush=True)
    for sid in sheets:
        print(f"http://127.0.0.1:{args.port}/sheet/{sid}?s={sheet_sig(TOKEN, sid)}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
