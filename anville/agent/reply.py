"""A designer's reply becomes constraints (ADR 0012).

The model reads the reply (Muse Spark Open, one `read_reply` call). Code accepts a rule only for a face group
the review raised, and only with a quote copied from the reply: a rule can't be invented, and nothing is read
out of the free text by code.
"""

from __future__ import annotations

import re

from anville.agent.review import Emit, _obj, _Runner, _tool

READ_TOOL = _tool(
    "read_reply",
    "The decisions the designer's reply makes about the face groups the review raised.",
    _obj(["rules"], {"rules": {"type": "array", "items": _obj(["face_group", "rule", "quote"], {
        "face_group": {"type": "string"},
        "rule": {"type": "string", "enum": ["keep", "may_change"]},
        "quote": {"type": "string", "description": "the designer's words that make the decision, copied exactly"},
        "prefer": {"type": "string", "enum": ["shop_fix", "part_edit"],
                   "description": "only with may_change, and only when the reply explicitly asks for one kind of fix"},
        "prefer_quote": {"type": "string", "description": "the designer's words that ask for it, copied exactly"},
    })}}),
)
INSTRUCTIONS = ("Read the designer's reply to the review. For each face group the reply decides, say whether it "
                "must stay as designed (keep) or may change (may_change), and quote the reply's words that say so. "
                "If a reply that lets the face change also explicitly asks for one kind of fix, give `prefer` and "
                "quote those words too: a part edit (\"delete it\", \"remove the chamfer\") or a shop fix (\"buy "
                "the cutter\", \"don't touch the model\"). Words that only allow a change (\"not needed\", \"it "
                "can go\", \"isn't critical\") are permission, not a request: they get no `prefer`. Thanks or "
                "approval of the review in general (\"looks good\", \"thanks\", \"sounds good\") decides nothing "
                "about a face. Decide nothing the reply doesn't say; a reply that decides nothing gets no rules.")

# Words that only acknowledge the review. A quote made of nothing else can't say what a face needs (Fireworks
# probe, ADR 0011 Amendment 1: "Thanks, looks good." was read as keep).
ACKNOWLEDGEMENT = frozenset("thanks thank you ty looks look good great sounds ok okay lgtm nice cool perfect "
                            "fine got it will do all the this that".split())


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
            if set(re.findall(r"[a-z]+", quote.casefold())) <= ACKNOWLEDGEMENT:
                return None, (f"{quote!r} only acknowledges the review: thanks or approval decides nothing about a "
                              f"face. Give {g} a rule only if the reply says what {g} needs")
            if g in seen:
                return None, f"one rule per face group: {g} has two"
            seen.add(g)
            rule_out = {"face_group": g, "rule": rule, "quote": quote}
            prefer, said = r.get("prefer"), r.get("prefer_quote") or ""
            if prefer:  # "unless the designer is clearly explicit" (Rayyan, 2026-10-31): only on their own words
                if rule != "may_change" or prefer not in ("shop_fix", "part_edit"):
                    return None, f"prefer is shop_fix or part_edit, and only on a may_change rule ({g})"
                if not said.strip() or _norm(said) not in _norm(reply):
                    return None, f"the words asking for a {prefer} on {g} must be copied from the reply"
                rule_out.update(prefer=prefer, prefer_quote=said)
            out.append(rule_out)
        return out, ""

    # The reply is quoted data, never instructions (ADR 0003 trust boundary).
    found = run.ask("read_reply", INSTRUCTIONS, {"review": raised}, {"reply": reply}, READ_TOOL, validate)
    return found, run


def constraint_entry(rule: dict, *, reply_id: str, review_key: str, version_id: str) -> dict:
    """The constraint ledger entry for one accepted rule, as {"payload", "provenance"} for append_ledger."""
    explicit = {"prefer": rule["prefer"], "prefer_reason": rule["prefer_quote"]} if rule.get("prefer") else {}
    return {"payload": {"face_group": rule["face_group"], "rule": rule["rule"], "reason": rule["quote"],
                        "version_id": version_id, **explicit},
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
