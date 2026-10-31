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


def constraints_from_ledger(rows: list[dict], *, version_id: str | None = None) -> list[dict]:
    """read_ledger rows -> tag_intents constraints, oldest first (the newest per group wins there). With
    `version_id`, only that version's: face labels are stable within one STEP only (Codex, PR #38)."""
    return [{"id": row["id"], **row["payload"]} for row in rows
            if version_id is None or row["payload"].get("version_id") == version_id]


def unrecorded(entries: list[dict], stored: list[dict]) -> list[dict]:
    """The entries not already in the ledger, by reply and face group: a retried job appends nothing twice, so
    an old reply can't become the newest rule (Codex, PR #38)."""
    seen = {(r["provenance"].get("comment"), r["payload"].get("face_group")) for r in stored}
    return [e for e in entries if (e["provenance"]["comment"], e["payload"]["face_group"]) not in seen]


def is_own_comment(info: dict, post_for_comment) -> bool:
    """Anville's own comment, from durable records: the posted table, or the header a comment carries when it
    went out on the owner's keys. The in-memory self: entry can be lost (Codex, PR #38, P1: a self-loop)."""
    from anville.connectors.onshape.oauth import KEYS_HEADER

    return post_for_comment(info.get("id", "")) is not None or (info.get("message") or "").startswith(KEYS_HEADER)


def resolve_review(parent_id: str, post_for_comment, parent_of, hops: int = 4) -> str | None:
    """The review event a thread answers. A parent that's Anville's re-plan leads back through the designer reply
    it answered, by durable records only, and in at most `hops` steps (Codex, PR #38)."""
    for _ in range(hops):
        post = post_for_comment(parent_id) if parent_id else None
        if post is None:
            return None
        key, kind = post
        if kind == "review":
            return key
        parent_id = parent_of(key.split(":", 1)[1])  # replan posts are keyed comment:<the reply they answered>
    return None


def nothing_read(groups: list[str], setups: int) -> str:
    """The code-rendered answer when a reply decides nothing: never a bare question, never a guessed rule."""
    names = " or ".join(groups) if len(groups) <= 2 else ", ".join(groups[:-1]) + f" or {groups[-1]}"
    return f"Anville read this reply, but not as a decision on {names}; the plan stands at {setups} setup(s)."
