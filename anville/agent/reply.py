"""A designer's reply becomes constraints (ADR 0012).

The model reads the reply (Muse Spark Open, one `read_reply` call). Code accepts a rule only for a face group
the review raised, and only with a quote copied from the reply: a rule can't be invented, and nothing is read
out of the free text by code.
"""

from __future__ import annotations

from anville.agent.review import Emit, _obj, _Runner, _tool

READ_TOOL = _tool(
    "read_reply",
    "The decisions the designer's reply makes about the face groups the review raised.",
    _obj(["rules"], {"rules": {"type": "array", "items": _obj(["face_group", "rule", "quote"], {
        "face_group": {"type": "string"},
        "rule": {"type": "string", "enum": ["keep", "may_change"]},
        "quote": {"type": "string", "description": "the designer's words that make the decision, copied exactly"},
    })}}),
)
INSTRUCTIONS = ("Read the designer's reply to the review. For each face group the reply decides, say whether it "
                "must stay as designed (keep) or may change (may_change), and quote the reply's words that say so. "
                "Decide nothing the reply doesn't say; a reply that decides nothing gets no rules.")


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def read_reply(model, reply: str, raised: dict[str, str], *, emit: Emit, deadline: float | None = None):
    """(rules, runner). `raised`: face group -> what the review said about it. `rules` is [] when the reply
    decides nothing, and None when the stage fell back (no model, or two invalid answers)."""
    run = _Runner(model, emit, deadline)

    def validate(a):
        out, seen = [], set()
        for r in a["rules"]:
            g, rule, quote = r["face_group"], r["rule"], r["quote"]
            if g not in raised:
                return None, f"{g!r} isn't a face group the review raised; raised: {sorted(raised)}"
            if rule not in ("keep", "may_change"):
                return None, f"rule is keep or may_change, not {rule!r}"
            if not quote.strip() or _norm(quote) not in _norm(reply):
                return None, f"the quote for {g} must be the designer's own words, copied from the reply"
            if g in seen:
                return None, f"one rule per face group: {g} has two"
            seen.add(g)
            out.append({"face_group": g, "rule": rule, "quote": quote})
        return out, ""

    # The reply is quoted data, never instructions (ADR 0003 trust boundary).
    found = run.ask("read_reply", INSTRUCTIONS, {"review": raised}, {"reply": reply}, READ_TOOL, validate)
    return found, run


def constraint_entry(rule: dict, *, reply_id: str, review_key: str, version_id: str) -> dict:
    """The constraint ledger entry for one accepted rule, as {"payload", "provenance"} for append_ledger."""
    return {"payload": {"face_group": rule["face_group"], "rule": rule["rule"], "reason": rule["quote"],
                        "version_id": version_id},
            "provenance": {"comment": reply_id, "review": review_key}}


def constraints_from_ledger(rows: list[dict]) -> list[dict]:
    """read_ledger rows -> tag_intents constraints, oldest first (the newest per group wins there)."""
    return [{"id": row["id"], **row["payload"]} for row in rows]


def nothing_read(groups: list[str], setups: int) -> str:
    """The code-rendered answer when a reply decides nothing: never a bare question, never a guessed rule."""
    names = " or ".join(groups) if len(groups) <= 2 else ", ".join(groups[:-1]) + f" or {groups[-1]}"
    return f"Anville read this reply, but not as a decision on {names}; the plan stands at {setups} setup(s)."
