"""Run the panel locally against a trace fixture, dripped out at its recorded pace (no store, no Onshape).

    uv run --with uvicorn python -m anville.panel.dev [fixture.json] [--speed 1.0]

Each event becomes visible once its recorded offset has passed (gaps longer than MAX_GAP_S are shortened,
so a fixture's older runs don't hold up the newest). Serves on 127.0.0.1:8765 with token "dev".
"""

import argparse
import json
import time
from pathlib import Path

from fastapi import FastAPI

from anville.panel import make_router

DEFAULT = Path(__file__).resolve().parents[2] / "tests/panel/fixtures/part_a_live.synthetic.json"
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
    args = ap.parse_args()
    data = json.loads(Path(args.fixture).read_text())
    app = FastAPI()
    app.include_router(make_router(dripping_reader(data["events"], args.speed), lambda *a, **k: True,
                                   panel_secret=TOKEN, allowed_docs=lambda: {data["document_id"]}))
    print(f"http://127.0.0.1:{args.port}/panel?documentId={data['document_id']}&t={TOKEN}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
