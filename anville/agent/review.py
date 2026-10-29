"""The review loop: a stage graph with a gated action space (ADR 0009, RFC 0001).

    Facts (code) → Triage (model) → Intent (code) → Options (model) → Decide (model + gate) → Communicate (model)

The model decides what is worth doing: which computed issues to raise, which edit kinds to try,
what to propose, ask or leave alone, and how to say it. Code decides what is allowed and computes
every number: issues come from the SetupPlan, part edits on protected faces never reach the
planner, `ask` exists only where the answer would change the plan, and numbers in the comment are
slots filled from tool results. Each model stage is one tool call, retried once, then replaced by
that stage's fixed-script step, so a bad answer degrades one stage, not the review.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from anville.agent.model import ChatModel, ModelUnavailable
from anville.geometry import PartFacts, SetupPlan, ShopProfile, min_setups
from anville.geometry.edits import CATALOG, NEEDS_GROUP, Option, evaluate_edits, feature_faces

PART_EDITS = frozenset({"remove_feature", "set_depth", "set_min_radius"})
FORCES_SETUP_MAX_GROUPS = 2   # a setup that exists for this many face groups or fewer is an issue
MAX_MODEL_CALLS = 8           # ADR 0003
MAX_COMMENT_CHARS = 1200
NOTHING_TO_RAISE = "Anville reviewed this version: it machines in {{plan.setups}} setup(s)."
# Writing prose costs more reasoning than picking from a menu: the live run on Part A ran out of
# tokens at 4096 in the communicate stage (2026-10-28), so that stage gets more room.
STAGE_MAX_TOKENS = {"triage": 4096, "options": 4096, "decide": 4096, "communicate": 8192}

Emit = Callable[..., None]    # emit(kind, title, **detail): ADR 0007 trace events


@dataclass
class Issue:
    id: str
    kind: str          # forces_setup | unreachable
    face_group: str
    detail: dict


@dataclass
class Intent:
    status: str        # protected | free | unknown
    reason: str = ""
    source: str = ""   # cad_feature_name | drawing | ledger:<id>


@dataclass
class Decision:
    issue_id: str
    action: str        # propose | ask | quiet
    option_key: str | None = None
    question: str | None = None
    reason: str = ""


@dataclass
class ReviewResult:
    plan: SetupPlan
    issues: list[Issue]
    intents: dict[str, Intent]
    selected: list[str]
    options: dict[str, Option]
    rejected: list[dict]
    ask_allowed: dict[str, bool]
    decisions: list[Decision]
    comment: str
    model_calls: int = 0
    fallbacks: list[str] = field(default_factory=list)


# --- code stages ---------------------------------------------------------------


def find_issues(facts: PartFacts, plan: SetupPlan) -> list[Issue]:
    """Issues are computed, never invented (ADR 0009 §2)."""
    group_of = {fid: g for g, ids in facts.labelled_groups().items() for fid in ids}
    issues: list[Issue] = []
    if len(plan.setups) > 1:
        main = max(plan.setups, key=lambda s: len(s.faces))
        for setup in plan.setups:
            groups = sorted({group_of[a.face_id] for a in setup.faces}, key=_group_order)
            if setup is main or not 0 < len(groups) <= FORCES_SETUP_MAX_GROUPS:
                continue
            for g in groups:
                issues.append(Issue(f"I{len(issues) + 1}", "forces_setup", g,
                                    {"directions": setup.directions, "groups_in_setup": groups}))
    by_group: dict[str, list[str]] = {}
    for u in plan.unreachable:
        by_group.setdefault(group_of[u.face_id], []).append(u.reason)
    for g in sorted(by_group, key=_group_order):
        issues.append(Issue(f"I{len(issues) + 1}", "unreachable", g, {"reasons": sorted(set(by_group[g]))}))
    return issues


def tag_intents(issues: list[Issue], stated: dict[str, str] | None = None,
                constraints: list[dict] | None = None) -> dict[str, Intent]:
    """protected | free | unknown per issue face group. A ledger constraint outranks a stated reason.

    `stated`: face group -> reason the design gives (CAD feature name, drawing note).
    `constraints`: ledger entries {"id", "face_group", "rule": "keep" | "may_change", "reason"}, oldest first.
    """
    stated, constraints = stated or {}, constraints or []
    # Every group with something on record is tagged, not just issue groups: a part edit on one
    # group can reach a protected neighbour (Greptile, PR #14).
    groups = [i.face_group for i in issues] + list(stated) + [c["face_group"] for c in constraints
                                                             if c.get("face_group")]
    out: dict[str, Intent] = {}
    for g in dict.fromkeys(groups):
        intent = Intent("unknown")
        if g in stated:
            intent = Intent("protected", stated[g], "cad_feature_name")
        for c in constraints:  # the newest constraint for the group wins
            if c.get("face_group") == g:
                status = "protected" if c.get("rule") == "keep" else "free"
                intent = Intent(status, c.get("reason", ""), f"ledger:{c.get('id', '?')}")
        out[g] = intent
    return out


def touched_groups(facts: PartFacts | None, req: dict) -> list[str]:
    """Groups a request changes. A part edit acts on a feature: the named group plus concave neighbours."""
    group = req.get("face_group")
    if group is None:
        return []
    if facts is None or req["kind"] not in PART_EDITS:
        return [group]
    faces = feature_faces(facts, group)
    return [g for g, ids in facts.labelled_groups().items() if set(ids) & faces]


def filter_requests(requests: list[dict], intents: dict[str, Intent],
                    facts: PartFacts | None = None) -> tuple[list[dict], list[dict]]:
    """ADR 0002 Amendment 1: a part edit touching a protected group is rejected, not scored."""
    kept, rejected = [], []
    for req in requests:
        hits = [g for g in touched_groups(facts, req)
                if req["kind"] in PART_EDITS and intents.get(g) and intents[g].status == "protected"]
        if hits:
            why = "; ".join(f"{g} is protected: {intents[g].reason}" for g in hits)
            rejected.append({**req, "reason": why})
        else:
            kept.append(req)
    return kept, rejected


def all_requests(group: str) -> list[dict]:
    return [{"kind": k, "face_group": group} for k in NEEDS_GROUP] + [{"kind": "route_machine"}]


def best_option(options: list[Option], *, part_edits: bool) -> Option | None:
    """The first plan-changing option in evaluate_edits order (fewest unreachable, then setups)."""
    for o in options:
        if o.changes_plan and (part_edits or o.side != "part"):
            return o
    return None


def ask_allowed(facts: PartFacts, shop: ShopProfile, issue: Issue, intent: Intent, *, axes: int = 3) -> bool:
    """The value-of-information gate (ADR 0009 §5): ask only if the answer changes the best option."""
    if intent.status != "unknown":
        return False
    options = evaluate_edits(facts, shop, all_requests(issue.face_group), axes=axes).options
    free, kept = best_option(options, part_edits=True), best_option(options, part_edits=False)
    return (free.key if free else None) != (kept.key if kept else None)


# --- slots ---------------------------------------------------------------------

_SLOT = re.compile(r"\{\{([^{}]+)\}\}")
_ALLOWED_DIGITS = re.compile(r"\b[GI]\d+\b|\b[35]-axis\b|[+-][XYZ]\b")


def slot_values(plan: SetupPlan, issues: list[Issue], options: dict[str, Option]) -> dict[str, str]:
    values = {"plan.setups": str(plan.count), "plan.unreachable": str(len(plan.unreachable))}
    for i in issues:
        values[f"issue.{i.id}.directions"] = " + ".join(i.detail.get("directions", []))
    for key, o in options.items():
        values[f"option.{key}.setups_before"] = str(o.before.setups)
        values[f"option.{key}.setups_after"] = str(o.after.setups)
        values[f"option.{key}.unreachable_after"] = str(o.after.unreachable)
    return values


def fill_slots(text: str, values: dict[str, str], option_keys: list[str]) -> tuple[str | None, str]:
    """Fill {{slots}}; reject unknown slots and any digit the model wrote itself (locked decision 3)."""
    unknown = [s for s in _SLOT.findall(text) if s.strip() not in values]
    if unknown:
        return None, f"unknown slot(s): {', '.join(unknown)}"
    bare = _SLOT.sub("", text)
    for key in sorted(option_keys, key=len, reverse=True):
        bare = bare.replace(key, "")
    bare = _ALLOWED_DIGITS.sub("", bare)
    if re.search(r"\d", bare):
        return None, "numbers must come from slots; the text contains a number written by the model"
    return _SLOT.sub(lambda m: values[m.group(1).strip()], text), ""


# --- model stages --------------------------------------------------------------

RULES = (
    "You are Anville, a manufacturing engineer reviewing a CNC-machined aluminum part for a shop. "
    "Code computes every number. Never write a digit yourself: use the {{slots}} you are given. "
    "Refer to faces by their group labels (G1, G2, ...). Describe a face's intent exactly as given in "
    "`intents`: call it protected only if its status is protected, and say its purpose is unknown if it is "
    "unknown. Call exactly one tool."
)


def _obj(required: list[str], props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props}


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


_PICK = {"type": "array", "items": _obj(["issue_id", "why"], {"issue_id": {"type": "string"},
                                                              "why": {"type": "string", "maxLength": 300}})}
TRIAGE_TOOL = _tool("triage", "Choose which computed issues are worth raising. Every issue is selected or dropped.",
                    _obj(["selected", "dropped"], {"selected": _PICK, "dropped": _PICK}))
OPTIONS_TOOL = _tool(
    "evaluate_edits", "Ask the planner to score edits. Name only an edit kind and a face group; code picks values.",
    _obj(["requests"], {"requests": {"type": "array", "minItems": 1, "maxItems": 12, "items": _obj(
        ["kind"], {"kind": {"type": "string", "enum": list(CATALOG)}, "face_group": {"type": "string"}})}}))
DECIDE_TOOL = _tool(
    "decide", "For each selected issue: propose an offered option, ask the designer (only where allowed), or stay quiet.",
    _obj(["decisions"], {"decisions": {"type": "array", "items": _obj(
        ["issue_id", "action", "reason"],
        {"issue_id": {"type": "string"}, "action": {"type": "string", "enum": ["propose", "ask", "quiet"]},
         "option_key": {"type": "string"}, "question": {"type": "string", "maxLength": 400},
         "reason": {"type": "string", "maxLength": 300}})}}))
COMMENT_TOOL = _tool("write_comment", "Write the review comment for the designer, using {{slots}} for every number.",
                     _obj(["text"], {"text": {"type": "string", "maxLength": MAX_COMMENT_CHARS}}))


class _Runner:
    def __init__(self, model: ChatModel | None, emit: Emit):
        self.model, self.emit, self.calls, self.fallbacks = model, emit, 0, []

    def ask(self, stage: str, instructions: str, context: dict, tool: dict, validate) -> object | None:
        """One tool call, retried once; None means the stage falls back to its fixed-script step."""
        name, error = tool["function"]["name"], "no model configured"
        for attempt in (1, 2):
            if self.model is None or self.calls >= MAX_MODEL_CALLS:
                error = error if self.model is None else "call cap reached"
                break
            content = json.dumps(context, sort_keys=True)
            if attempt == 2:
                content += f"\n\nYour previous answer was rejected: {error}. Call {name} again."
            messages = [{"role": "system", "content": f"{RULES}\n\n{instructions}"},
                        {"role": "user", "content": content}]
            self.calls += 1
            try:
                reply = self.model.call(messages, [tool], max_tokens=STAGE_MAX_TOKENS.get(stage))
            except ModelUnavailable as e:
                error = f"model unavailable: {e}"
                break
            if reply.error:
                error = reply.error
            elif reply.tool != name:
                error = f"called {reply.tool!r}, expected {name!r}"
            else:
                try:
                    value, error = validate(reply.arguments)
                except (KeyError, TypeError, AttributeError) as e:  # arguments that ignore the schema
                    value, error = None, f"arguments don't match the {name} schema ({type(e).__name__}: {e})"
                if not error:
                    self.emit("tool_call", "Model answered", stage=stage, tool=name, attempt=attempt)
                    return value
            self.emit("error", "Model answer rejected", stage=stage, tool=name, attempt=attempt, reason=error)
        self.fallbacks.append(stage)
        self.emit("error", "Stage fell back to the fixed script", stage=stage, reason=error)
        return None


def run_review(facts: PartFacts, shop: ShopProfile, model: ChatModel | None, *,
               stated: dict[str, str] | None = None, constraints: list[dict] | None = None,
               emit: Emit | None = None, axes: int = 3) -> ReviewResult:
    """Review one part version. `model=None` runs every model stage as its fixed-script step."""
    emit = emit or (lambda *a, **k: None)
    run = _Runner(model, emit)

    # Facts (code)
    plan = min_setups(facts, shop, axes=axes)
    issues = find_issues(facts, plan)
    by_id = {i.id: i for i in issues}
    emit("verify", "Setup plan and issues computed", stage="facts", setups=plan.count,
         issues=[{"id": i.id, "kind": i.kind, "face_group": i.face_group} for i in issues])
    base = {"setups": [{"directions": s.directions,
                        "groups": sorted({g for g, ids in facts.labelled_groups().items()
                                          if set(ids) & {a.face_id for a in s.faces}}, key=_group_order)}
                       for s in plan.setups],
            "issues": [{"id": i.id, "kind": i.kind, "face_group": i.face_group, **i.detail} for i in issues]}
    if not issues:
        text, _ = fill_slots(NOTHING_TO_RAISE, slot_values(plan, issues, {}), [])
        return ReviewResult(plan, [], {}, [], {}, [], {}, [], text or "", run.calls, run.fallbacks)

    # Triage (model)
    def v_triage(a):
        sel, drop = [p["issue_id"] for p in a["selected"]], [p["issue_id"] for p in a["dropped"]]
        if set(sel) & set(drop) or sorted(sel + drop) != sorted(by_id):
            return None, "every issue must be selected or dropped exactly once"
        return sel, ""
    selected = run.ask("triage", "Choose which issues are worth the designer's attention.", base,
                       TRIAGE_TOOL, v_triage)
    if selected is None:
        selected = [i.id for i in issues]
    emit("decision", "Issues chosen", stage="triage", selected=selected)
    if not selected:
        # Nothing chosen: the later stages would have nothing to work on (live run, 2026-10-28).
        text, _ = fill_slots(NOTHING_TO_RAISE, slot_values(plan, [], {}), [])
        return ReviewResult(plan, issues, {}, [], {}, [], {}, [], text or "", run.calls, run.fallbacks)

    # Intent (code)
    chosen = [by_id[i] for i in selected]
    intents = tag_intents(chosen, stated, constraints)
    gates = {i.id: ask_allowed(facts, shop, i, intents[i.face_group], axes=axes) for i in chosen}
    intent_view = {g: {"status": t.status, "reason": t.reason} for g, t in intents.items()}
    emit("verify", "Intent tagged and ask gate computed", stage="intent", intents=intent_view, ask_allowed=gates)

    # Options (model)
    groups = {i.face_group for i in chosen}

    def v_options(a):
        for r in a["requests"]:
            if set(r) - {"kind", "face_group"}:
                return None, f"name only a kind and a face group; code picks values (got {sorted(set(r))})"
            if r["kind"] not in CATALOG:
                return None, f"{r['kind']!r} is not in the catalog {list(CATALOG)}"
            if "face_group" in r and r["face_group"] not in groups:
                return None, f"face_group must be one of the selected issues' groups {sorted(groups)}"
            if r["kind"] in NEEDS_GROUP and r.get("face_group") not in groups:
                return None, f"{r['kind']} must name one of the selected issues' groups {sorted(groups)}"
        return a["requests"], ""
    ctx = {**base, "selected": selected, "intents": intent_view}
    requests = run.ask("options", "Choose which edit kinds the planner should score for the selected issues. "
                       "Part edits touching a protected group will be rejected; shop-side edits are always allowed.",
                       ctx, OPTIONS_TOOL, v_options)
    if requests is None:
        requests = [r for g in sorted(groups, key=_group_order) for r in all_requests(g)]
    kept, rejected = filter_requests(requests, intents, facts)
    result = evaluate_edits(facts, shop, kept, axes=axes) if kept else None
    options = {o.key: o for o in (result.options if result else [])}
    for r in rejected:
        emit("tool_result", "Edit rejected: the face's intent is stated", stage="options",
             edit_kind=r["kind"], face_group=r.get("face_group"), reason=r["reason"])
    for o in options.values():
        emit("option", "Option scored", stage="options", key=o.key, side=o.side, setups_before=o.before.setups,
             setups_after=o.after.setups, changes_plan=o.changes_plan)

    # Decide (model + gate)
    issue_of_option = {k: o.face_group for k, o in options.items()}

    values = slot_values(plan, chosen, options)
    keys = list(options)

    def v_decide(a):
        out = []
        if sorted(d["issue_id"] for d in a["decisions"]) != sorted(selected):
            return None, "make exactly one decision per selected issue"
        for d in a["decisions"]:
            issue = by_id[d["issue_id"]]
            if d["action"] not in ("propose", "ask", "quiet"):
                return None, f"unknown action {d['action']!r}"
            if d["action"] == "propose":
                key = d.get("option_key")
                if key not in options:
                    return None, f"{key!r} was not offered; offered: {sorted(options)}"
                if issue_of_option[key] not in (None, issue.face_group):
                    return None, f"{key} belongs to another face group"
            question = d.get("question")
            if d["action"] == "ask":
                if not gates[issue.id] or not question:
                    return None, f"asking about {issue.id} is not allowed: the answer would not change the plan"
                question, err = fill_slots(question, values, keys)  # the designer reads it: same number rule
                if err:
                    return None, f"question for {issue.id}: {err}"
            out.append(Decision(d["issue_id"], d["action"], d.get("option_key"), question, d["reason"]))
        return out, ""
    offered = [{"key": k, "side": o.side, "face_group": o.face_group, "setups_before": o.before.setups,
                "setups_after": o.after.setups, "unreachable_after": o.after.unreachable,
                "changes_plan": o.changes_plan} for k, o in options.items()]
    decisions = run.ask("decide", "Decide what to do about each selected issue.",
                        {**ctx, "offered_options": offered, "rejected_requests": rejected, "ask_allowed": gates},
                        DECIDE_TOOL, v_decide)
    if decisions is None:
        decisions = [_fallback_decision(by_id[i], gates[i], list(options.values())) for i in selected]
    for d in decisions:
        emit("decision", "Decision made", stage="decide", issue_id=d.issue_id, action=d.action,
             option_key=d.option_key)

    # Communicate (model)
    asking = any(d.action == "ask" for d in decisions)

    def v_comment(a):
        text, err = fill_slots(a["text"], values, keys)
        if not err and "?" in text and not asking:
            # The comment may only ask what Decide was allowed to ask (Greptile, PR #14).
            return None, "the comment asks a question, but no decision asks one; state the decisions instead"
        return text, err
    comment = run.ask("communicate", "Write the comment the designer will read: two to four plain sentences. "
                      "Use only these slots for numbers.",
                      {**ctx, "decisions": [d.__dict__ for d in decisions], "slots": sorted(values)},
                      COMMENT_TOOL, v_comment)
    if comment is None:
        comment, _ = fill_slots(_fallback_comment(decisions, by_id), values, keys)
    if not comment:  # the template is built from validated parts; this is a last resort, never an empty post
        comment, _ = fill_slots(NOTHING_TO_RAISE, values, keys)
    return ReviewResult(plan, issues, intents, selected, options, rejected, gates, decisions,
                        comment or "", run.calls, run.fallbacks)


# --- fixed-script steps ------------------------------------------------------------


def _fallback_decision(issue: Issue, may_ask: bool, options: list[Option]) -> Decision:
    if may_ask:
        return Decision(issue.id, "ask", question=f"Is {issue.face_group} required as modelled?",
                        reason="its intent is unknown and the answer changes the best plan")
    mine = [o for o in options if o.face_group in (None, issue.face_group)]
    best = best_option(mine, part_edits=True)
    if best:
        return Decision(issue.id, "propose", option_key=best.key, reason="the best plan-changing option offered")
    return Decision(issue.id, "quiet", reason="no option changes the plan")


def _fallback_comment(decisions: list[Decision], by_id: dict[str, Issue]) -> str:
    lines = ["Anville reviewed this version: it machines in {{plan.setups}} setup(s)."]
    for d in decisions:
        g = by_id[d.issue_id].face_group
        if d.action == "ask":
            lines.append(f"{g}: {d.question}")
        elif d.action == "propose":
            lines.append(f"{g}: {d.option_key} takes it from {{{{option.{d.option_key}.setups_before}}}} "
                         f"to {{{{option.{d.option_key}.setups_after}}}} setup(s).")
    return "\n".join(lines)


def _group_order(label: str) -> int:
    return int(label[1:])
