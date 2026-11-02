"""Anville on a folder of STEP files, without Onshape (ADR 0001's LocalSource and LocalSink).

    python scripts/anville_local.py review   parts/bracket.step [--shop NAME]
    python scripts/anville_local.py reply    parts/bracket.step "Keep G3, it's the weld clearance." [--shop NAME]
    python scripts/anville_local.py shop-note parts/bracket.step --arrived 910480 [--shop NAME]

Each part's review lands next to it: <part>.review.md (the comment thread), <part>.sheet.json (the setup sheet)
and <part>.trace.jsonl (the trace the panel reads). Models run when their keys are in the environment or .env
(ADR 0004); without them, the review is code's, from the geometry alone. ANVILLE_AGENT=off forces that.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anville.connectors.local import LocalError, LocalSink, LocalSource, loop  # noqa: E402
from anville.env import REPO_ROOT, load_env  # noqa: E402


def env() -> dict[str, str]:
    """The process environment over .env, so a variable set for one run wins."""
    dotenv = REPO_ROOT / ".env"
    return {**(load_env(dotenv) if dotenv.is_file() else {}), **os.environ}


def main(argv: list[str] | None = None) -> int:
    shop = "the shop whose ledger this run reads and writes (default: dll); a new name starts as the demo shop was"
    p = argparse.ArgumentParser(prog="anville_local", description=__doc__.split("\n\n")[0])
    p.add_argument("--shop", default="dll", help=shop)
    # Also after the command, as the usage shows (Codex P2); SUPPRESS keeps a subcommand from resetting it.
    after = argparse.ArgumentParser(add_help=False)
    after.add_argument("--shop", default=argparse.SUPPRESS, help=shop)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", parents=[after], help="review a part's current version")
    r.add_argument("part")
    y = sub.add_parser("reply", parents=[after], help="answer the review as the designer: Anville re-plans")
    y.add_argument("part")
    y.add_argument("text")
    s = sub.add_parser("shop-note", parents=[after], help="the machinist's note on the part's setup sheet")
    s.add_argument("part")
    s.add_argument("--arrived", nargs="*", default=[], help="cutters from the sheet that arrived, e.g. 910480")
    s.add_argument("--different", metavar="TEXT", help="the run went differently: what happened, in words")
    s.add_argument("--sheet", metavar="ID", help="the sheet it answers, from its file name (default: the latest)")
    a = p.parse_args(argv)

    path = Path(a.part).resolve()
    sink = LocalSink(path.parent, shop=a.shop)
    try:
        part = LocalSource(path.parent).part(path.name)
        if a.cmd == "review":
            e = env()
            keys = ("MUSE_SPARK_OPEN_API_KEY", "MODEL_API_KEY", "FIREWORKS_API_KEY")
            if e.get("ANVILLE_AGENT", "on") == "on" and not any(e.get(k) for k in keys):
                print("No model keys found: the review is code's, from the geometry alone.", file=sys.stderr)
            print(loop.review(part, sink, e))
            print(f"\nWrote {part.name}.review.md, {part.name}.sheet.json and {part.name}.trace.jsonl "
                  f"in {path.parent}")
        elif a.cmd == "reply":
            answer = loop.reply(part, sink, env(), a.text)
            print(answer or "Anville read nothing to act on in that reply, so it stays quiet.")
        else:
            outcome, text = ("different", a.different) if a.different else ("as_planned", "")
            entries = loop.shop_note(part, sink, arrived=a.arrived, outcome=outcome, text=text, sheet=a.sheet)
            got = [e["payload"]["tool"]["name"] for e in entries if e["payload"].get("kind") == "tool_acquired"]
            print("Shop note recorded." + (f" The shop now owns {', '.join(got)}; the next review plans with it."
                                           if got else ""))
    except (LocalError, ValueError) as e:
        print(f"anville_local: {e}", file=sys.stderr)
        if "not on this sheet" in str(e):  # only a review that decided to buy a cutter puts it on the sheet
            print("The sheet lists the cutters its review decided to buy; a review without models buys none.",
                  file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
