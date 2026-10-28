"""Tool-calling probe (ADR 0003): does the model return valid tool calls at least 9/10 times?

Two tools from the ADR 0003 tool layer, with real JSON schemas. Twenty synthetic review
situations, one request each. A call is *valid* when the model returns exactly one tool
call whose name is known and whose arguments parse as JSON and pass the schema. We also
report, separately, whether it picked the tool we expected and only used face groups that
exist (grounding). Below 90% valid (9/10 in ADR 0003; 18/20 here), the fixed-order script becomes the primary path.

Usage: uv run python scripts/probe_tool_calls.py [open|spark|both] [--effort low] [--out results.json]
Stdlib only. Reads keys from .env; never prints them.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anville.env import load_env  # noqa: E402

# Muse Spark Open reasons before calling a tool (~800 reasoning tokens observed),
# so a 1024 cap truncates it before the call is emitted.
MAX_TOKENS = 4096

EDIT_KINDS = ["set_min_radius", "set_depth", "add_tool", "add_fixture", "route_machine"]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "evaluate_edits",
            "description": (
                "Score candidate edits to the part or the shop with the deterministic planner. "
                "Returns setups and total $ at quantity for each edit. Edits must come from the catalog."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["edits"],
                "properties": {
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind"],
                            "properties": {
                                "kind": {"type": "string", "enum": EDIT_KINDS},
                                "face_group": {"type": "string", "pattern": "^G[0-9]+$"},
                                "value_mm": {"type": "number", "exclusiveMinimum": 0},
                                "machine_id": {"type": "string"},
                            },
                        },
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_designer",
            "description": (
                "Ask the designer one question in the comment thread, pinned to face groups, "
                "when design intent is unknown and the answer changes which fix is right."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "face_groups"],
                "properties": {
                    "question": {"type": "string", "minLength": 10, "maxLength": 400},
                    "face_groups": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "pattern": "^G[0-9]+$"},
                    },
                },
            },
        },
    },
]

SYSTEM = (
    "You are Anville, a manufacturing engineer reviewing a CNC-machined aluminum part. "
    "Code computes every number; you never invent numbers. Always act by calling exactly one tool. "
    "Use evaluate_edits to score fixes. Use ask_designer only when design intent is unknown and "
    "the answer decides which fix is right. Refer only to face groups listed in the facts."
)

SHOP = (
    "Shop: 3-axis VMC 'vmc1', 5-axis 'umc5'. End mills on hand: 6, 10, 12 mm (L:D 4). "
    "Setup cost $85. Quantity 50."
)

# (situation, expected tool). Facts are synthetic and computed-looking; numbers are illustrative.
CASES = [
    ("G1 internal pocket corners R1.5 mm need a 3 mm tool; smallest on hand is 6 mm. Pocket is non-mating.",
     "evaluate_edits"),
    ("G2 slot is 40 mm deep, 8 mm wide: needs 8 mm tool at L:D 5. Drawing marks G2 depth as a fit dimension.",
     "ask_designer"),
    ("Holes G3 (x6, 6.76 mm) reached only from +Z; side hole G4 forces a second setup from +X.",
     "evaluate_edits"),
    ("G5 boss has a 0.5 mm corner radius. The designer's note says 'G5 sharp corner seals against gasket'.",
     "ask_designer"),
    ("Part needs 3 setups on vmc1 (+Z, -Z, +X). On umc5 it needs 1. umc5 rate is 1.8x vmc1.",
     "evaluate_edits"),
    ("G6 pocket floor depth 22 mm with 6 mm corner radius. No drawing. Pocket is a weight-reduction cut.",
     "evaluate_edits"),
    ("G7 bore has R0.8 mm internal edge. Two prior parts in this project used R3 on the same bore with no complaints.",
     "evaluate_edits"),
    ("G8 thin wall 0.9 mm between pockets G9 and G10. Unknown whether G8 is structural.",
     "ask_designer"),
    ("Undercut G11 is unreachable from all 6 directions on vmc1 or umc5 with any listed tool.",
     "evaluate_edits"),
    ("G12 and G13 are mirrored pockets each with R2 corners; 4 mm tool would fit, shop has none under 6 mm.",
     "evaluate_edits"),
    ("G14 has two M3 tapped holes 18 mm deep; longest tap on hand reaches 12 mm.",
     "evaluate_edits"),
    ("G15 pocket corners R2.5. Designer on v3: 'a mating part's square corner sits in G15'. "
     "Unknown whether a corner relief is acceptable.",
     "ask_designer"),
    ("Part needs 2 setups (+Z, -Z). G16 chamfer on the bottom edge is the only face needing -Z.",
     "evaluate_edits"),
    ("G17 bore is 22 mm H7, 30 mm deep; the boring bar on hand reaches 25 mm.",
     "evaluate_edits"),
    ("G18 lightening pockets (x4) leave 0.8 mm floors. No drawing, no notes. Part is a motor mount.",
     "ask_designer"),
    ("G19 hole is reached only from +Y and G20 only from -Y; everything else from +Z. "
     "A 4th-axis fixture spec exists for vmc1.",
     "evaluate_edits"),
    ("G21 edge has a 0.4 mm fillet the drawing labels 'break edge, optional'.",
     "evaluate_edits"),
    ("G22 slot is 5.9 mm wide, 18 mm deep; a 6 mm tool does not fit. On a similar part in this "
     "project the designer said slot width is clearance only.",
     "evaluate_edits"),
    ("G23 boss is 4 mm diameter, 25 mm tall, unsupported. Unknown whether G23 is a locating pin or cosmetic.",
     "ask_designer"),
    ("Quantity rises from 50 to 500. Part needs 2 setups on vmc1; a dedicated fixture would hold 6 parts per setup.",
     "evaluate_edits"),
]


def _validate(value, schema, path="$") -> list[str]:
    """Minimal JSON Schema subset: type, enum, pattern, required, additionalProperties, bounds."""
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "number": (int, float)}
    if t and not isinstance(value, types[t]) or (t == "number" and isinstance(value, bool)):
        return [f"{path}: expected {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if t == "string":
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: {value!r} fails pattern")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            errs.append(f"{path}: length {len(value)} out of bounds")
    if t == "number" and "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
        errs.append(f"{path}: {value} not > {schema['exclusiveMinimum']}")
    if t == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10**9):
            errs.append(f"{path}: {len(value)} items out of bounds")
        for i, item in enumerate(value):
            errs += _validate(item, schema.get("items", {}), f"{path}[{i}]")
    if t == "object":
        props = schema.get("properties", {})
        errs += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errs += [f"{path}: unexpected {k}" for k in value if k not in props]
        for k, v in value.items():
            if k in props:
                errs += _validate(v, props[k], f"{path}.{k}")
    return errs


def _face_groups(args: dict) -> set[str]:
    groups = set(args.get("face_groups", []))
    groups |= {e["face_group"] for e in args.get("edits", []) if isinstance(e, dict) and "face_group" in e}
    return groups


def _call(base_url: str, key: str, model: str, situation: str, effort: str | None = None) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{SHOP}\nFacts: {situation}"},
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": MAX_TOKENS,
    }
    if effort:
        # Meta Model API: top-level reasoning_effort; max_tokens caps reasoning + output combined.
        body["reasoning_effort"] = effort
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def judge(response: dict, situation: str, expected: str) -> dict:
    choice = response["choices"][0]
    msg = choice["message"]
    calls = msg.get("tool_calls") or []
    out = {"n_calls": len(calls), "valid": False, "expected_tool": False, "grounded": False, "errors": [],
           "finish_reason": choice.get("finish_reason"),
           "completion_tokens": (response.get("usage") or {}).get("completion_tokens")}
    if len(calls) != 1:
        out["content"] = msg.get("content")
        out["errors"].append(f"{len(calls)} tool calls; finish={out['finish_reason']}; "
                             f"content={str(msg.get('content'))[:100]!r}")
        return out
    fn = calls[0]["function"]
    out["tool"] = fn["name"]
    schema = next((t["function"]["parameters"] for t in TOOLS if t["function"]["name"] == fn["name"]), None)
    if schema is None:
        out["errors"].append(f"unknown tool {fn['name']!r}")
        return out
    try:
        args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
    except json.JSONDecodeError as e:
        out["raw_arguments"] = fn["arguments"]
        out["errors"].append(f"arguments not JSON: {e}")
        return out
    out["args"] = args
    out["raw_arguments"] = fn["arguments"]
    out["errors"] = _validate(args, schema)
    out["valid"] = not out["errors"]
    out["expected_tool"] = fn["name"] == expected
    allowed = set(re.findall(r"G[0-9]+", situation))
    out["grounded"] = _face_groups(args) <= allowed
    return out


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Lower end of the 95% Wilson interval: how low the true valid rate could plausibly be."""
    if n == 0:
        return 0.0
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def run(label: str, base_url: str, key: str, model: str, effort: str | None = None) -> list[dict]:
    rows = []
    for i, (situation, expected) in enumerate(CASES, 1):
        t0 = time.monotonic()
        try:
            res = judge(_call(base_url, key, model, situation, effort), situation, expected)
        except urllib.error.HTTPError as e:
            res = {"valid": False, "expected_tool": False, "grounded": False,
                   "errors": [f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"]}
        except Exception as e:  # timeouts, malformed responses
            res = {"valid": False, "expected_tool": False, "grounded": False, "errors": [repr(e)[:200]]}
        res.update(case=i, expected=expected, seconds=round(time.monotonic() - t0, 1))
        rows.append(res)
        mark = "OK " if res["valid"] else "BAD"
        print(f"[{label}] {i:2d} {mark} tool={res.get('tool', '-'):<15} expected={expected:<15} "
              f"grounded={res['grounded']!s:<5} {res['seconds']:>5}s tok={res.get('completion_tokens', '-')!s:>5}  {'; '.join(res['errors'])[:100]}")
    n = len(rows)
    v = sum(r["valid"] for r in rows)
    e = sum(r["expected_tool"] for r in rows)
    g = sum(r["grounded"] for r in rows if r["valid"])
    secs = sorted(r["seconds"] for r in rows)
    bar = math.ceil(0.9 * n)
    print(f"[{label}] valid {v}/{n} (95% lower bound {wilson_lower(v, n):.0%}) | expected tool {e}/{n} | "
          f"grounded {g}/{v} | median {secs[n // 2]}s, max {secs[-1]}s | "
          f"{'PASS' if v >= bar else 'FAIL'} (bar: {bar}/{n})")
    return rows


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    effort = sys.argv[sys.argv.index("--effort") + 1] if "--effort" in sys.argv else None
    env = load_env()
    targets = {
        "open": ("muse-spark-open", env["MUSE_SPARK_OPEN_BASE_URL"], env["MUSE_SPARK_OPEN_API_KEY"],
                 env["MUSE_SPARK_OPEN_MODEL_ID"]),
        "spark": ("muse-spark", "https://api.meta.ai/v1", env["MODEL_API_KEY"], env["MUSE_SPARK_MODEL_ID"]),
    }
    results = {}
    for name in (["open", "spark"] if which == "both" else [which]):
        label, base, key, model = targets[name]
        if not key:
            print(f"[{label}] skipped: API key is empty in .env")
            continue
        tag = f"{label}@{effort}" if effort else label
        results[f"{model}@{effort}" if effort else model] = run(tag, base, key, model, effort)
    if out_path:
        Path(out_path).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
