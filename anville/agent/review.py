"""The review loop: a stage graph with a gated action space (ADR 0009, RFC 0001).

    Facts + Intent (code) → Plan (model) → Decide + Write (model + gate) → Check (code)
        → Verify (model #2) → Publish

The model decides what is worth doing: which computed issues to raise, which edit kinds to try,
what to propose, ask or leave alone, and how to explain it to the designer. Code decides what is
allowed and computes every number: issues come from the SetupPlan, part edits on protected faces
never reach the planner, `ask` exists only where the answer would change the plan, and numbers
are slots filled from tool results.

The comment is part of the graph. The model writes it as sentences that each cite the nodes they
rest on (issues, intents, options, its own decisions). Code checks each sentence's structure; a
second model checks that it says no more than it cites. Only verified sentences are published;
any decision not stated by a verified sentence gets a line rendered by code, and questions are
always placed by code from accepted asks. Three model calls per review; each is retried once and
then replaced by that stage's fixed-script step.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Callable

from anville.agent.model import ChatModel, ModelUnavailable
from anville.geometry import PartFacts, SetupPlan, ShopProfile, min_setups
from anville.geometry.edits import CATALOG, NEEDS_GROUP, Option, evaluate_edits, feature_faces

PART_EDITS = frozenset({"remove_feature", "set_depth", "set_min_radius"})
FORCES_SETUP_MAX_GROUPS = 2   # a setup that exists for this many face groups or fewer is an issue
MAX_MODEL_CALLS = 8           # ADR 0003
MIN_CALL_S = 10               # less time left than this: the stage falls back instead of calling
NOTHING_TO_RAISE = "Anville reviewed this version: it machines in {{plan.setups}} setup(s)."
STAGE_MAX_TOKENS = {"triage": 4096, "options": 4096, "decide": 4096}

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
    prefer: str = ""   # on a freed face, the side the designer explicitly asked for: shop_fix | part_edit | ""


@dataclass
class Decision:
    issue_id: str
    action: str        # propose | quiet
    option_key: str | None = None
    # Only on a proposal, and only where the gate allows: flag, then suggest (ADR 0009 Amendment 1).
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
    usage: list[dict] = field(default_factory=list)  # per call: stage, model, host, prompt/completion/cached tokens


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
                intent = Intent(status, c.get("reason", ""), f"ledger:{c.get('id', '?')}",
                                c.get("prefer", "") if status == "free" else "")
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


def shop_side_requests(group: str) -> list[dict]:
    return [{"kind": "add_tool", "face_group": group}, {"kind": "route_machine"}]


def all_requests(group: str) -> list[dict]:
    return [{"kind": k, "face_group": group} for k in NEEDS_GROUP] + [{"kind": "route_machine"}]


def best_option(options: list[Option], *, part_edits: bool) -> Option | None:
    """The first plan-changing option in evaluate_edits order (fewest unreachable, then setups)."""
    for o in options:
        if o.changes_plan and (part_edits or o.side != "part"):
            return o
    return None


def ask_allowed(facts: PartFacts, shop: ShopProfile, issue: Issue, intent: Intent, *,
                intents: dict[str, Intent] | None = None, axes: int = 3, back_chamfer=()) -> bool:
    """The value-of-information gate (ADR 0009 §5): ask only if the answer changes the best option.

    A part edit that would also change a protected neighbour isn't a real option whatever the
    designer answers, so it's dropped before comparing (the same check as filter_requests).
    """
    if intent.status != "unknown":
        return False
    intents = intents or {}
    options = [o for o in evaluate_edits(facts, shop, all_requests(issue.face_group), axes=axes,
                                         back_chamfer=back_chamfer).options
               if o.side != "part" or not any(
                   g != issue.face_group and intents.get(g) and intents[g].status == "protected"
                   for g in touched_groups(facts, {"kind": o.kind, "face_group": o.face_group}))]
    free, kept = best_option(options, part_edits=True), best_option(options, part_edits=False)
    return (free.key if free else None) != (kept.key if kept else None)


# --- slots ---------------------------------------------------------------------

_SLOT = re.compile(r"\{\{([^{}]+)\}\}")
_COST_CLAIM = re.compile(r"\bcheap(?:est|er)\b|\bleast expensive\b|\bcosts? less\b|\bless expensive\b"
                         r"|\blowest[- ]cost\b|\bcosts? the least\b", re.I)
_INTERNAL = re.compile(r"\b(?:issue|intent|option|decision):|\b(?:intent|option|issue|plan)\.\w|\bI\d+\b")
_ALLOWED_DIGITS = re.compile(r"\b[GI]\d+\b|\b[35]-axis\b|[+-][XYZ]\b")


def describe_group(facts: PartFacts, group: str) -> str | None:
    """A code-measured phrase for a face group ("the 1.5 mm chamfer on the bottom front edge"), so prose can name
    the face the way a designer would. From geometry's describe_group once it lands; None until then."""
    try:
        from anville.geometry import describe_group as measured
        return measured(facts, group)
    except Exception:  # not landed yet, or a geometry error (unknown label): the face goes undescribed,
        return None    # the review still posts (geometry lane, 2026-10-31)


def slot_values(plan: SetupPlan, issues: list[Issue], options: dict[str, Option],
                intents: dict[str, "Intent"] | None = None) -> dict[str, str]:
    values = {"plan.setups": str(plan.count), "plan.unreachable": str(len(plan.unreachable))}
    for g, t in (intents or {}).items():  # stated reasons come from CAD or the ledger, not the model
        if t.reason:
            values[f"intent.{g}.reason"] = t.reason
    for i in issues:
        values[f"issue.{i.id}.directions"] = " + ".join(i.detail.get("directions", []))
    for key, o in options.items():
        values[f"option.{key}.setups_before"] = str(o.before.setups)
        values[f"option.{key}.setups_after"] = str(o.after.setups)
        if o.kind == "add_tool":  # tool names carry digits: they reach the designer through a slot
            values[f"option.{key}.tool"] = str(o.params.get("name", key))
        values[f"option.{key}.fix"] = _fix(key, o)  # the fix in code's words, so prose names it exactly
        values[f"option.{key}.unreachable_before"] = str(o.before.unreachable)
        values[f"option.{key}.unreachable_after"] = str(o.after.unreachable)
    return values


def slot_source(slot: str, nodes: dict) -> str | None:
    """The node a slot's value comes from: option.<key>.<field> -> option:<key>, issue.<id>.<field> -> issue:<id>,
    plan.<field> -> plan. Option keys contain dots and slashes, so the field is the last segment."""
    head, _, rest = slot.partition(".")
    if head in ("plan", "costs"):
        return head if head in nodes else None
    node = f"{head}:{rest.rsplit('.', 1)[0]}" if "." in rest else None
    return node if node in nodes else None


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
    def fill(line: str) -> str:  # a phrase opening a line: "the left Ø8.5 through hole" -> "The left ..."
        out = _SLOT.sub(lambda m: values[m.group(1).strip()], line)
        return out[:1].upper() + out[1:] if line.startswith("{{") else out
    return "\n".join(fill(line) for line in text.split("\n")), ""  # a whole comment is filled at once


# --- model stages --------------------------------------------------------------

# One stable system prompt for every call: with the facts first in the user message, each call in a
# review shares a long identical prefix, which the host can cache (Baseten reported cached prompt
# tokens in the probe). Stage-specific text goes last.
RULES = (
    "You are Anville, a manufacturing engineer reviewing a CNC-machined aluminum part for a shop. "
    "Code computes every number. Never write a digit yourself: use the {{slots}} you are given. "
    "Refer to faces by their group labels (G1, G2, ...). Describe a face's intent exactly as given in "
    "`intents`: call it protected only if its status is protected, and say its purpose is unknown if it is "
    "unknown. An option with side 'shop' changes the shop (tools, machines, fixtures), never the part's "
    "geometry, so it is always compatible with a protected face. A protected face is therefore never a "
    "reason to drop an issue: the fix may be on the shop side. Flag, then suggest: every issue you raise "
    "comes with a proposed fix in the same reply, and a question never replaces the proposal, except for an "
    "issue listed in `ask_only`, where no shop-side fix exists and you ask instead. "
    "Call exactly one tool."
)
STAGE_MAX_TOKENS = {"plan": 6144, "decide": 6144, "verify": 4096}  # plan hit 4096 live; decide also writes
# Live, 2026-10-29 (Spark Open, Part A, 8 reviews): "low" cut latency from 21-33 s to 4-17 s, but at
# low effort planning once dropped the main issue. Planning keeps the model's default; deciding
# and writing (4/4 right at low) run at low. The verifier brings its own setting.
STAGE_EFFORT = {"plan": None, "decide": "low", "verify": None}
MAX_SENTENCES = 8


def _obj(required: list[str], props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props}


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


_PICK = {"type": "array", "items": _obj(["issue_id", "why"], {"issue_id": {"type": "string"},
                                                              "why": {"type": "string", "maxLength": 300}})}
PLAN_TOOL = _tool(
    "plan", "Choose which issues to raise (every issue is selected or dropped) and which edit kinds the planner "
    "should score for the selected ones. Name only a kind and a face group; code picks the values.",
    _obj(["selected", "dropped", "requests"], {
        "selected": _PICK, "dropped": _PICK,
        "requests": {"type": "array", "maxItems": 12, "items": _obj(
            ["kind"], {"kind": {"type": "string", "enum": list(CATALOG)}, "face_group": {"type": "string"}})}}))
_SENTENCE = _obj(["text", "role", "cites"], {
    "text": {"type": "string", "maxLength": 300},
    "role": {"type": "string", "enum": ["context", "action", "reason"]},
    "cites": {"type": "array", "minItems": 1, "items": {"type": "string"}}})
DECIDE_TOOL = _tool(
    "decide", "For each selected issue, propose an offered option that changes the plan, or stay quiet. Where "
    "ask_allowed is true and the face's purpose is unknown, give the proposal a `question`, in your own words, "
    "asking the designer what the face is for or whether it is needed as modelled. Ask about its purpose only: "
    "never name an edit such as removing it, since code states the part edit beside your question. The comment "
    "flags the issue and suggests the fix in one reply, without waiting for an answer. Only for an issue listed in `ask_only` "
    "(no shop-side fix exists, and the only fix is a part edit on a face whose purpose is unknown), choose "
    "`ask` with a question and no option: code then states the part edit as a conditional. Then write the "
    "comment the designer will read: sentences that each cite the nodes they rest on. You may state a fix's "
    "payback (its whole range, with its payback slots) and name a rival fix (cite it with the cost ranking); "
    "a rival named with the ranking gets its own sentence opening with its fix slot, saying it can come out "
    "ahead at some shop rates; "
    "a lead the ranking gives at mid-range is said to lead at mid-range shop rates, never cheapest, cheaper or "
    "lowest-cost. Name a fix with its {{option.<key>.fix}} slot, never a part number, an option key or a "
    "paraphrase. The slot fills with a phrase such as 'buying the cutter', so make it the subject that opens the "
    "sentence: '{{option.<key>.fix}} takes it to {{option.<key>.setups_after}} setup(s).' Put no verb before it "
    "(not 'Use', 'Fix with' or 'Proposing') and don't repeat the tool after it. "
    "Code states whatever your verified sentences leave out. Questions are written only in `decisions`; code "
    "places them.",
    _obj(["decisions", "comment"], {
        "decisions": {"type": "array", "items": _obj(
            ["issue_id", "action", "reason"],
            {"issue_id": {"type": "string"}, "action": {"type": "string", "enum": ["propose", "ask", "quiet"]},
             "option_key": {"type": "string"}, "question": {"type": "string", "maxLength": 400},
             "reason": {"type": "string", "maxLength": 300}})},
        "comment": {"type": "array", "maxItems": MAX_SENTENCES, "items": _SENTENCE}}))
VERIFY_TOOL = _tool(
    "verify", "For each sentence: is everything it states supported by the nodes it cites, does it ask for or "
    "suggest nothing beyond the accepted decisions, and, if its role is action, does it state the cited "
    "decision's action (what changes), not just something true about it?",
    _obj(["verdicts"], {"verdicts": {"type": "array", "items": _obj(
        ["index", "supported", "why"], {"index": {"type": "integer"}, "supported": {"type": "boolean"},
                                        "why": {"type": "string", "maxLength": 200}})}}))


class _Runner:
    def __init__(self, model: ChatModel | None, emit: Emit, deadline: float | None = None):
        self.model, self.emit, self.calls, self.fallbacks, self.usage = model, emit, 0, [], []
        self.deadline = deadline

    def ask(self, stage: str, instructions: str, base: dict, extra: dict, tool: dict, validate,
            model: ChatModel | None = None) -> object | None:
        """One tool call, retried once; None means the stage falls back to its fixed-script step."""
        model = model or self.model
        name, error = tool["function"]["name"], "no model configured"
        effort = STAGE_EFFORT.get(stage)
        for attempt in (1, 2):
            if model is None or self.calls >= MAX_MODEL_CALLS:
                error = error if model is None else "call cap reached"
                break
            left = None if self.deadline is None else self.deadline - time.monotonic()
            if left is not None and left < MIN_CALL_S:
                error = "time budget spent"
                break
            content = (json.dumps(base, sort_keys=True) + f"\n\nSTAGE {stage}: {instructions}\n"
                       + json.dumps(extra, sort_keys=True))
            if attempt == 2:
                content += f"\n\nYour previous answer was rejected: {error}. Call {name} again."
            messages = [{"role": "system", "content": RULES}, {"role": "user", "content": content}]
            self.calls += 1
            # Announced before the call, so the panel sees the stage start while the model works (Codex, PR #24).
            self.emit("tool_call", "Model called", stage=stage, model=model.name,
                      host=getattr(model, "host", None), attempt=attempt)
            record = {"stage": stage, "model": model.name, "host": getattr(model, "host", None)}
            started = time.monotonic()
            try:
                reply = model.call(messages, [tool], max_tokens=STAGE_MAX_TOKENS.get(stage),
                                   reasoning_effort=effort, timeout_s=left)
            except ModelUnavailable as e:
                # Failed calls are often the slowest: their time belongs in the record too (Codex, PR #24).
                self.usage.append({**record, "host": getattr(model, "host", None),
                                   "latency_s": round(time.monotonic() - started, 3), "error": str(e)})
                error = f"model unavailable: {e}"
                break
            # Read after the call: a failover model names the host that served it (ADR 0011 Amendment 1).
            self.usage.append({**record, "host": getattr(model, "host", None),
                               "latency_s": round(time.monotonic() - started, 3), **(reply.usage or {})})
            if reply.finish_reason == "length":  # RFC 0001 budget policy: the retry thinks less (gate on #26)
                effort = "low"
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
               verifier: ChatModel | None = None, stated: dict[str, str] | None = None,
               constraints: list[dict] | None = None, emit: Emit | None = None, axes: int = 3,
               deadline: float | None = None, back_chamfer=()) -> ReviewResult:
    """Review one part version in three model calls: plan, decide-and-write, verify.

    `model=None` runs every model stage as its fixed-script step. Without a `verifier`, no model
    prose is published: the comment is rendered by code. `deadline` is a `time.monotonic()` value
    that no model call runs past; once too little time is left, stages fall back to code.
    `back_chamfer` is `assess_back_chamfer` for the shop's cutters plus the catalog's; the plan, the
    options and the ask gate all get this one list, so they can't disagree about what's reachable.
    """
    emit = emit or (lambda *a, **k: None)
    run = _Runner(model, emit, deadline)

    # Facts and intent (code), for every issue up front so planning is one call
    plan = min_setups(facts, shop, axes=axes, back_chamfer=back_chamfer)
    issues = find_issues(facts, plan)
    by_id = {i.id: i for i in issues}
    emit("verify", "Setup plan and issues computed", stage="facts", setups=plan.count,
         issues=[{"id": i.id, "kind": i.kind, "face_group": i.face_group} for i in issues])
    if not issues:
        text, _ = fill_slots(NOTHING_TO_RAISE, slot_values(plan, issues, {}), [])
        return ReviewResult(plan, [], {}, [], {}, [], {}, [], text or "", run.calls, run.fallbacks, run.usage)
    intents = tag_intents(issues, stated, constraints)
    gates = {i.id: ask_allowed(facts, shop, i, intents[i.face_group], intents=intents, axes=axes,
                             back_chamfer=back_chamfer) for i in issues}
    intent_view = {g: {"status": t.status, "reason": t.reason} for g, t in intents.items()}
    emit("verify", "Intent tagged and ask gate computed", stage="intent", intents=intent_view, ask_allowed=gates)
    base = {"setups": [{"directions": s.directions,
                        "groups": sorted({g for g, ids in facts.labelled_groups().items()
                                          if set(ids) & {a.face_id for a in s.faces}}, key=_group_order)}
                       for s in plan.setups],
            "issues": [{"id": i.id, "kind": i.kind, "face_group": i.face_group, **i.detail} for i in issues]}
    # Planning sees geometry only. Live (2026-10-29), planning that saw "protected, ask not allowed"
    # dropped the main issue in 3 of 6 stated-intent runs; triage that saw geometry only never did.
    # Intent reaches the model at Decide, after the filter has rejected protected part edits.

    # Plan (model): triage and the planner requests in one call
    def v_plan(a):
        sel, drop = [x["issue_id"] for x in a["selected"]], [x["issue_id"] for x in a["dropped"]]
        if set(sel) & set(drop) or sorted(sel + drop) != sorted(by_id):
            return None, "every issue must be selected or dropped exactly once"
        if not sel:  # code found these issues; raising none of them left a bare setup count (gate on #26)
            return None, "select at least one issue: each one costs this part a setup or a reachable face"
        groups = {by_id[i].face_group for i in sel}
        for r in a["requests"]:
            if set(r) - {"kind", "face_group"}:
                return None, f"name only a kind and a face group; code picks values (got {sorted(set(r))})"
            if r["kind"] not in CATALOG:
                return None, f"{r['kind']!r} is not in the catalog {list(CATALOG)}"
            if ("face_group" in r or r["kind"] in NEEDS_GROUP) and r.get("face_group") not in groups:
                return None, f"face_group must be one of the selected issues' groups {sorted(groups)}"
        return (sel, a["requests"]), ""
    planned = run.ask("plan", "Choose the issues worth the designer's attention and the edit kinds to score, "
                      "both part-side and shop-side.", base, {}, PLAN_TOOL, v_plan)
    if planned is None:
        selected = [i.id for i in issues]
        requests = [r for i in issues for r in all_requests(i.face_group)]
    else:
        selected, requests = planned
        # Shop-side fixes are always scored: Plan sees geometry only, so it can't know the shop could buy a
        # catalog cutter (live, 2026-10-29: it asked for route and remove only, 4 of 4, and the 910480 was
        # never offered). The model still chooses which part-side edits are worth scoring.
        requests = requests + [r for i in selected for r in shop_side_requests(by_id[i].face_group)
                               if r not in requests]
        # Part edits too, where the designer's answer could change the plan (the ask gate is open): without
        # them `ask_only` and the conditional line never exist (gate on #26: undercut went quiet 3 of 10).
        requests = requests + [r for i in selected if gates[i] for r in all_requests(by_id[i].face_group)
                               if r not in requests]
        # And on a face the designer freed: its part edits are ranked with the shop fixes, so they must be scored
        # whatever Plan asked for (ADR 0010 Amendment 1; Codex, PR #41).
        requests = requests + [r for i in selected if intents[by_id[i].face_group].status == "free"
                               for r in all_requests(by_id[i].face_group) if r not in requests]
    emit("decision", "Issues chosen", stage="plan", selected=selected)
    if not selected:
        # Nothing chosen: the later stages would have nothing to work on (live run, 2026-10-28).
        text, _ = fill_slots(NOTHING_TO_RAISE, slot_values(plan, [], {}), [])
        return ReviewResult(plan, issues, intents, [], {}, [], gates, [], text or "", run.calls, run.fallbacks,
                            run.usage)
    chosen = [by_id[i] for i in selected]
    kept, rejected = filter_requests(requests, intents, facts)
    result = evaluate_edits(facts, shop, kept, axes=axes, back_chamfer=back_chamfer) if kept else None
    options = {o.key: o for o in (result.options if result else [])}
    for r in rejected:
        emit("tool_result", "Edit rejected: the face's intent is stated", stage="plan",
             edit_kind=r["kind"], face_group=r.get("face_group"), reason=r["reason"])
    priced, costed = _price(facts, shop, plan, options, emit)
    for o in options.values():
        # The comment's own wording, slots filled, so the panel never re-derives names from keys (panel lane).
        name = _name(o).replace(f"{{{{option.{o.key}.tool}}}}", str(o.params.get("name", o.key)))
        emit("option", "Option scored", stage="plan", key=o.key, name=name, edit_kind=o.kind, params=dict(o.params),
             side=o.side, setups_before=o.before.setups, setups_after=o.after.setups,
             changes_plan=o.changes_plan, **priced.get(o.key, {}))
    ranked, considered = _rank(costed, shop, chosen, options, emit, intents)
    # A lead that wins only on the tie-break must not read as the cheapest (ADR 0010 §5): code names its rivals.
    rivals = {r.lead: [k for k in r.top if k != r.lead] for r in ranked.values() if r.lead and r.basis == "mid_range"}

    # Intent unknown: a fix that works whatever the face is for leads (council ruling, ADR 0009 Amendment 1).
    # A part edit then appears only as code's conditional line, next to the question.
    # Every part edit on such a face is barred from leading and from the model's prose (Codex, PR #23).
    # With no shop-side fix, a part edit still never leads: the review flags and asks, and code states the edit
    # as a conditional (Rayyan's ruling, 2026-10-30, lane 1; lane 3 adds its dollars).
    conditional, barred, ask_only = {}, {}, set()
    for i in chosen:
        mine = [o for o in options.values() if o.face_group in (None, i.face_group)]
        shop_fix = best_option(mine, part_edits=False)
        if intents[i.face_group].status == "unknown" and (shop_fix or gates[i.id]):
            barred[i.id] = {o.key for o in mine if o.side == "part"}
            part = next((o for o in mine if o.side == "part" and o.changes_plan), None)
            if part:
                conditional[i.id] = part.key
                if not shop_fix and gates[i.id]:
                    ask_only.add(i.id)

    # Decide and write (model + gate): decisions are validated; the prose is checked sentence by sentence
    values = slot_values(plan, chosen, options, intents)
    where = {i.id: w for i in chosen if (w := describe_group(facts, i.face_group))}
    values.update({f"issue.{i}.where": w for i, w in where.items()})  # digits in it reach prose through the slot
    estimates = {}  # conditional part edit -> its cost slot, where only a part edit helps (ruling, lane 3)
    if priced:
        q = max(shop.costs.scenarios.quantities)
        values["costs.quantity"] = str(q)
        for issue_id, key in conditional.items():
            if issue_id in ask_only and key in priced and str(q) in priced[key]["costs"]:
                lo, hi = priced[key]["costs"][str(q)]
                values[f"option.{key}.cost_lo"], values[f"option.{key}.cost_hi"] = f"{lo:,.0f}", f"{hi:,.0f}"
                # Say so wherever dollars rest on placeholder inputs (shop profile rule; Codex, PR #26).
                estimates[key] = priced[key]["placeholder"]
    paybacks = {}  # key -> (payback, payback_mid), for options that buy a tool
    for key, d in priced.items():
        if d.get("payback") is not None:
            lo, hi = d["payback"]
            paybacks[key] = ((lo, hi), d["payback_mid"])
            for name, v in (("payback_lo", lo), ("payback_hi", hi), ("payback_mid", d["payback_mid"])):
                if v is not None:
                    values[f"option.{key}.{name}"] = str(v)
    keys = list(options)
    nodes = comment_nodes(plan, chosen, intents, options, priced=priced, ranked=ranked)
    for i, w in where.items():  # the verifier sees what the slot says
        nodes[f"issue:{i}"]["where"] = w
    if "costs.quantity" in values:  # the quantity code prices at, so a sentence using it can be verified
        nodes["costs"] = {"quantity": int(values["costs.quantity"]), "note": "the part count the costs are given at"}

    def v_decide(a):
        out = []
        if sorted(d["issue_id"] for d in a["decisions"]) != sorted(selected):
            return None, "make exactly one decision per selected issue"
        for d in a["decisions"]:
            issue = by_id[d["issue_id"]]
            if d["action"] == "ask" and issue.id not in ask_only:
                return None, (f"don't only ask about {issue.id}: propose an offered option, and put the question "
                              "in that proposal's `question` field (flag, then suggest)")
            if d["action"] == "ask" and not d.get("question"):
                return None, f"an ask about {issue.id} needs its question"
            if d["action"] not in ("propose", "ask", "quiet"):
                return None, f"unknown action {d['action']!r}"
            if d["action"] == "propose":
                key = d.get("option_key")
                if key not in options:
                    return None, f"{key!r} was not offered; offered: {sorted(options)}"
                if options[key].face_group not in (None, issue.face_group):
                    return None, f"{key} belongs to another face group"
                if not options[key].changes_plan:  # eval gate, PR #23: "takes it from 1 to 1 setup(s)"
                    return None, f"{key} changes nothing: propose an option whose changes_plan is true, or stay quiet"
                rank = ranked.get(issue.id)
                if rank and key in considered[issue.id] and rank.lead and key != rank.lead:
                    return None, (f"{rank.lead} costs less at mid-range inputs and is in the top tier for "
                                  f"{issue.face_group}: lead with it (ADR 0010)")
                if rank and key in considered[issue.id] and not rank.lead and key not in rank.top:
                    return None, f"{key} costs more than {rank.top} at every input: lead with one of those (ADR 0010)"
                prefer = intents[issue.face_group].prefer
                unwanted = {"shop_fix": "part", "part_edit": "shop"}.get(prefer)
                wanted_exists = any(o.changes_plan and o.side != unwanted and o.face_group in (None, issue.face_group)
                                    for o in options.values())
                if unwanted == options[key].side and wanted_exists:
                    return None, (f"the designer explicitly asked for a {prefer.replace('_', ' ')} on "
                                  f"{issue.face_group}: lead with one")
                if key in barred.get(issue.id, ()):
                    return None, (f"{issue.face_group}'s purpose is unknown: lead with a fix that works whatever it's "
                                  f"for (a shop-side option); a part edit appears only as code's conditional line")
            question = d.get("question") or None
            if question:
                if d["action"] == "quiet" or not gates[issue.id]:
                    return None, f"asking about {issue.id} is not allowed: the answer would not change the plan"
                _, err = fill_slots(question, values, keys)  # the designer reads it: same number rule
                if err:
                    return None, f"question for {issue.id}: {err}"
                # Kept as a template, so rendering re-checks slots, not filled numbers (Greptile, PR #14).
            out.append(Decision(d["issue_id"], d["action"], d.get("option_key"), question, d["reason"]))
        for i, sent in enumerate(a["comment"]):  # inside the stage, so a malformed entry is retried, not a crash
            if not (isinstance(sent, dict) and isinstance(sent.get("text"), str)
                    and sent.get("role") in ("context", "action", "reason") and isinstance(sent.get("cites"), list)
                    and all(isinstance(c, str) for c in sent["cites"])):
                return None, f"comment entry {i} needs text (string), role (context|action|reason) and cites (strings)"
            if rivals and _COST_CLAIM.search(sent["text"]):
                return None, (f"comment entry {i} ranks the fixes by cost, but the lead wins only at mid-range inputs: "
                              "don't call a fix cheapest, cheaper or lowest-cost; say it leads at mid-range shop "
                              "rates, or leave the costs to code")
        return (out, list(a["comment"])), ""
    offered = [{"key": k, "side": o.side, "face_group": o.face_group, "setups_before": o.before.setups,
                "setups_after": o.after.setups, "unreachable_after": o.after.unreachable,
                "changes_plan": o.changes_plan} for k, o in options.items()]
    decided = run.ask("decide", "Decide each selected issue, then write the comment for the designer. Put node "
                      "ids only in `cites` (cite decision:<issue_id> for your own decisions, and cost:<issue_id> "
                      "for a claim about cost), never in the text. "
                      "Use only these slots for numbers. A sentence proposing a fix states its result with "
                      "that option's setups_after slot, e.g. \"Removing G3 takes it to "
                      "{{option.remove_feature/G3.setups_after}} setup(s).\"",
                      base, {"selected": selected, "intents": intent_view, "ask_allowed": gates,
                             "ask_only": sorted(ask_only),
                             "cost_ranking": {i: {"top": list(r.top), "lead": r.lead, "basis": r.basis}
                                              for i, r in ranked.items()},
                             "offered_options": offered, "rejected_requests": rejected,
                             "nodes": sorted(nodes), "slots": sorted(values)}, DECIDE_TOOL, v_decide)
    if decided is None:
        decisions = [_fallback_decision(by_id[i], gates[i], list(options.values()), ranked.get(i), considered.get(i, ()),
                                        intents[by_id[i].face_group].prefer) for i in selected]
        sentences = []
    else:
        decisions, sentences = decided
    decisions = _with_intent_questions(decisions, by_id, intents, gates)
    for d in decisions:
        emit("decision", "Decision made", stage="decide", issue_id=d.issue_id, action=d.action,
             option_key=d.option_key, question=d.question, reason=d.reason)  # trace and panel

    # Check (code), then verify (a second model): only grounded, verified prose is published
    decided_by = {d.issue_id: d for d in decisions}
    checked = []
    for idx, sent in enumerate(sentences[:MAX_SENTENCES]):
        err = check_sentence(sent, decided_by, nodes, values, keys, conditional_only=set().union(*barred.values()),
                             options_by_key=options)
        emit("verify" if not err else "error", "Sentence checked", stage="check", index=idx, ok=not err,
             reason=err or "", text=fill_slots(sent["text"], values, keys)[0] or sent["text"])  # the panel shows it
        if not err:
            checked.append(sent)
    # A question the model wrote is prose too: verified like a sentence, or code's own question goes instead
    # (shop lane, 2026-10-31: it was published after only a slot check).
    asked = [{"text": d.question, "role": "question", "issue_id": d.issue_id,
              "cites": [f"issue:{d.issue_id}", f"intent:{by_id[d.issue_id].face_group}"]}
             for d in decisions if d.question and d.question != _intent_question(by_id[d.issue_id].face_group)]
    verified = verify_sentences(run, verifier, checked + asked, decided_by, nodes, values, keys, base, emit)
    supported = {s["issue_id"] for s in verified if s.get("role") == "question"}
    verified = [s for s in verified if s.get("role") != "question"]
    for i, d in enumerate(decisions):
        if d.question and any(a["issue_id"] == d.issue_id for a in asked) and d.issue_id not in supported:
            decisions[i] = replace(d, question=_intent_question(by_id[d.issue_id].face_group))
            d = decisions[i]  # re-sent as "Decision made", the event the panel's card reads (Codex, PR #45)
            emit("decision", "Decision made", stage="verify", issue_id=d.issue_id, action=d.action,
                 option_key=d.option_key, question=d.question, reason=d.reason, question_replaced=True)
    decided_by = {d.issue_id: d for d in decisions}
    comment, _ = fill_slots(assemble_comment(verified, decisions, by_id, intents, options, conditional, paybacks,
                                             estimates, rivals), values, keys)
    if not comment:  # every part was validated, so this is a last resort: never an empty post
        comment, _ = fill_slots(NOTHING_TO_RAISE, values, keys)
    emit("verify", "Comment assembled", stage="publish", model_sentences=len(verified))
    return ReviewResult(plan, issues, intents, selected, options, rejected, gates, decisions,
                        comment or "", run.calls, run.fallbacks, run.usage)


# --- the comment as grounded nodes ---------------------------------------------------


BASIS_MEANS = {  # what a ranking basis lets a sentence claim (ADR 0010); the verifier judges cost prose by it
    "dominates": "the lead costs less than every other ranked fix at every input in the shop's ranges",
    "mid_range": "the lead costs least with every ranged shop input at its midpoint, but it is not shown to cost "
                 "less than every other ranked fix at every input",
    "tie": "no ranked fix costs less than the others at every input or at the midpoints",
    "sole": "it is the only ranked fix that makes the whole part",
    "none": "no ranked fix makes the whole part",
}


def comment_nodes(plan: SetupPlan, chosen: list[Issue], intents: dict[str, Intent],
                  options: dict[str, Option], *, priced: dict | None = None,
                  ranked: dict | None = None) -> dict[str, dict]:
    """What a sentence may cite: id -> the fact it stands for (decision nodes are added per decision). Prices
    and the cost ranking are citable too, so a cost claim can be verified (models lane, 2026-10-31)."""
    nodes = {"plan": {"setups": plan.count, "unreachable": len(plan.unreachable)}}
    for i in chosen:
        nodes[f"issue:{i.id}"] = {"kind": i.kind, "face_group": i.face_group, **i.detail}
        nodes[f"decision:{i.id}"] = {"issue": i.id}
    for g, t in intents.items():
        nodes[f"intent:{g}"] = {"status": t.status, "reason": t.reason}
    for k, o in options.items():
        nodes[f"option:{k}"] = {"fix": _fix(k, o), "side": o.side, "face_group": o.face_group,
                                "setups_before": o.before.setups, "setups_after": o.after.setups,
                                "unreachable_after": o.after.unreachable}  # fix: code's name, so prose using it verifies
        if o.params.get("direction"):  # where the fix works from (models lane: prose put the cutter on -Z)
            nodes[f"option:{k}"]["works_from"] = o.params["direction"]
        price = (priced or {}).get(k)
        if price:
            nodes[f"option:{k}"].update({f: price.get(f) for f in ("one_time", "payback", "payback_mid", "unpriced",
                                                                   "placeholder")})
    for issue_id, r in (ranked or {}).items():
        nodes[f"cost:{issue_id}"] = {"top": list(r.top), "lead": r.lead, "basis": r.basis,
                                     "means": BASIS_MEANS.get(r.basis, "")}
    return nodes


def check_sentence(sent: dict, decided_by: dict[str, Decision], nodes: dict, values: dict,
                   keys: list[str], *, conditional_only: set[str] = frozenset(),
                   options_by_key: dict[str, Option] | None = None) -> str:
    """Code's half of the gate on prose. Returns why the sentence is rejected, or "" if it passes.

    `conditional_only`: part edits on faces of unknown purpose, which only code's conditional line names.
    """
    cites = sent.get("cites") or []
    named = [k for k in conditional_only if f"option:{k}" in cites or k in sent.get("text", "")]
    if named:
        return f"{named[0]} is offered only in code's conditional line while the face's purpose is unknown"
    if not cites or any(c not in nodes for c in cites):
        return f"cites unknown nodes {[c for c in cites if c not in nodes]}" if cites else "cites nothing"
    _, err = fill_slots(sent["text"], values, keys)
    if err:
        return err
    if "?" in sent["text"]:
        return "questions reach the designer only through an accepted ask"
    # Write for the designer: citations go in `cites` and numbers in {{slots}}, never in the words
    # (live, 2026-10-29: "per intent:G3", "(intent.G3.reason)", "Issue I1").
    if _INTERNAL.search(_SLOT.sub("", sent["text"])):
        return "write for the designer: node ids and slot names belong in cites and {{slots}}, not the text"
    cited = [decided_by[c.split(":", 1)[1]] for c in cites if c.startswith("decision:")]
    if sent["role"] == "action" and (len(cited) != 1 or cited[0].action == "ask"):
        return "an action sentence cites exactly one propose or quiet decision"
    if sent["role"] == "action" and cited[0].action == "propose":
        # It replaces the rendered proposal, so it must state the outcome, not just avoid overclaiming
        # (Greptile, PR #14: "G3 stays as designed." cited the decision and hid the machine change).
        slot = _result_slot(options_by_key[cited[0].option_key]) if options_by_key else \
            f"{{{{option.{cited[0].option_key}.setups_after}}}}"
        if slot not in sent["text"]:
            return f"an action sentence for a proposal states its result with {slot}"
    # Options the sentence may name: those of its cited decisions and the option nodes it cites.
    allowed = {d.option_key for d in cited if d.option_key} | {c.split(":", 1)[1] for c in cites
                                                               if c.startswith("option:")}
    words = _SLOT.sub("", sent["text"])  # slot names aren't mentions
    stray = [k for k in keys if k in words and k not in allowed]
    # Edit kinds count too: a rejected request (say, removing a protected face) is in no offered key.
    stray += [k for k in CATALOG if re.search(rf"\b{k}\b", words) and not any(o.startswith(k) for o in allowed)]
    if stray:
        return f"mentions options no cited decision accepted: {sorted(set(stray))}"
    return ""


def verify_sentences(run: "_Runner", verifier: ChatModel | None, checked: list[dict],
                     decided_by: dict[str, Decision], nodes: dict, values: dict, keys: list[str],
                     base: dict, emit: Emit) -> list[dict]:
    """The model's half of the gate on prose: does each sentence say more than it cites?"""
    if not checked:
        return []
    if verifier is None:
        emit("error", "No verifier: model prose is not published", stage="verify")
        return []
    items = []
    for i, sent in enumerate(checked):
        cited = {c: nodes[c] for c in sent["cites"]}
        for slot in _SLOT.findall(sent["text"]):  # a filled number brings its source, so it can't be disputed for
            src = slot_source(slot.strip(), nodes)  # lack of a citation (models lane, 2026-10-31: 32 of 54 drops)
            if src and src not in cited:
                cited[src] = nodes[src]
        for c in sent["cites"]:
            if c.startswith("decision:"):
                d = decided_by[c.split(":", 1)[1]]
                # No question here: it is verified on its own and may be replaced, so prose can't rest on it
                # (Codex, PR #45).
                cited[c] = {"action": d.action, "option_key": d.option_key}
                if d.option_key and f"option:{d.option_key}" in nodes:  # the decided fix comes with its decision
                    cited.setdefault(f"option:{d.option_key}", nodes[f"option:{d.option_key}"])
        if sent["role"] == "question":  # ruling (a): the question rides on the proposal; it needs no ask
            d = decided_by[sent["issue_id"]]
            cited[f"decision:{d.issue_id}"] = {"action": d.action, "option_key": d.option_key,
                                               "carries_this_question": True}
        items.append({"index": i, "text": fill_slots(sent["text"], values, keys)[0], "role": sent["role"],
                      "cites": cited})

    def v_verify(a):
        for v in a["verdicts"]:
            if not (isinstance(v, dict) and isinstance(v.get("index"), int) and isinstance(v.get("supported"), bool)
                    and isinstance(v.get("why"), str)):
                return None, "each verdict needs index (integer), supported (true or false) and why (string)"
        verdicts = {v["index"]: v for v in a["verdicts"]}
        if sorted(verdicts) != list(range(len(items))):
            return None, f"give one verdict per sentence index 0..{len(items) - 1}"
        return verdicts, ""
    decisions_view = [{"issue_id": d.issue_id, "action": d.action, "option_key": d.option_key,
                       **({"carries_question": True} if d.question else {})} for d in decided_by.values()]
    verdicts = run.ask("verify", "Judge each sentence strictly against its cited nodes and the accepted decisions. "
                       "A proposal on a face whose purpose is unknown may carry one question asking what the face "
                       "is for or whether it is needed as modelled (carries_question): such a question is part of "
                       "that accepted decision, not a request for an edit.",
                       base, {"accepted_decisions": decisions_view, "sentences": items}, VERIFY_TOOL, v_verify,
                       model=verifier)
    if verdicts is None:  # can't verify: publish no model prose
        return []
    out = []
    for i, sent in enumerate(checked):
        ok = bool(verdicts[i]["supported"])
        emit("verify" if ok else "error", "Sentence verified" if ok else "Sentence not supported",
             stage="verify", index=i, reason=verdicts[i]["why"], text=items[i]["text"])
        if ok:
            out.append(sent)
    return out


def assemble_comment(verified: list[dict], decisions: list[Decision], by_id: dict[str, Issue],
                     intents: dict[str, Intent], options: dict[str, Option],
                     conditional: dict[str, str] | None = None,
                     paybacks: dict[str, tuple] | None = None,
                     estimates: dict[str, bool] | None = None,  # estimates: key -> rests on placeholders
                     rivals: dict[str, list[str]] | None = None) -> str:  # rivals: tie-break lead -> the others
    """Verified model sentences in the model's order; every decision not stated by a verified action
    sentence gets its code-rendered line; accepted questions are always placed by code, after the
    proposal they belong to (flag, then suggest)."""
    covered = {c.split(":", 1)[1] for s in verified if s["role"] == "action"
               for c in s["cites"] if c.startswith("decision:")}
    # Code is the backstop, not the author (Rayyan, 2026-10-31): verified prose that used all of a fix's payback
    # slots already stated it, and a sentence citing a rival with the cost ranking already named that rival.
    # A rival is named only by a sentence about it: citing the ranking, opening with the rival's own name slot,
    # and saying it comes out ahead at some inputs. A mention, or "we lead, ahead of X", leaves out the caveat
    # code's line carries (Codex, PR review, three passes).
    named = {m.group(1) for s in verified if any(c.startswith("cost:") for c in s["cites"])
             and _COMPARES.search(s["text"]) and (m := _RIVAL_FIRST.match(s["text"]))}
    lines = [s["text"] for s in verified]
    if not any(s["role"] == "context" for s in verified):
        lines.insert(0, NOTHING_TO_RAISE)
    said = set()  # a part-wide fix (the 5-axis route) is named once, for the first issue it fixes
    for d in decisions:
        if d.issue_id not in covered and d.action == "propose" and d.option_key in said:
            lines.append(f"{by_id[d.issue_id].face_group}: the same change covers it.")
        elif d.issue_id not in covered:
            lines.append(render_line(d, by_id, intents, options))
        if d.action == "propose":
            said.add(d.option_key)
        if d.action == "propose" and d.option_key in (paybacks or {}):
            if not payback_covered(d.option_key, *paybacks[d.option_key], [s["text"] for s in verified]):
                lines.append(payback_line(d.option_key, *paybacks[d.option_key]))
        for r in (rivals or {}).get(d.option_key, []) if d.action == "propose" else []:  # ADR 0010 §5
            if r in named:
                continue
            name = _name(options[r])
            lines.append(f"{name[0].upper()}{name[1:]} costs less at some inputs.")
        if d.question:
            g, key = by_id[d.issue_id].face_group, (conditional or {}).get(d.issue_id)
            if key:  # the part edit, offered only on condition, next to the one question about intent
                also = " also" if d.action == "propose" else ""
                lines.append(f"If {g} isn't needed, {_name(options[key])}{also} {_effect(options[key], short=True)}.")
                if key in (estimates or {}):  # lane 3: what the edit would cost, from the cost model's slots
                    rates = " on placeholder shop rates" if estimates[key] else ""
                    lines.append(f"At {{{{costs.quantity}}}} parts, the plan after {_name(options[key])} runs "
                                 f"${{{{option.{key}.cost_lo}}}}–${{{{option.{key}.cost_hi}}}}{rates}; "
                                 "designer time isn't priced.")
            # An ask's own line already says the purpose isn't on record.
            flag = "" if d.action == "ask" else f"{g}'s purpose isn't on record. "
            lines.append(f"{flag}{d.question} The answer could change the best plan.")
    return "\n".join(lines)


def _price(facts: PartFacts, shop: ShopProfile, plan: SetupPlan, options: dict[str, Option],
           emit: Emit) -> tuple[dict, object | None]:
    """Each option's dollars from the shop lane's cost model (S7), for the trace and the slots; {} for a
    shop without cost inputs. A costing bug costs only the dollars, never the review. Also returns the
    CostResult, for ranking (ADR 0010)."""
    if getattr(shop, "costs", None) is None or not options:
        return {}, None
    try:
        from anville.shop import cost_options

        # Only options that change the plan are priced: the rest are noise in the panel (shop lane, #26).
        result = cost_options(facts, shop, [o for o in options.values() if o.changes_plan], plan=plan)
    except Exception as e:
        emit("error", "Costing failed; the review goes out without dollars", stage="plan", error=repr(e)[:300])
        return {}, None
    # The verdict per quantity ("depends" when recommended is None) and each input's status and source,
    # so the panel shows which dollars rest on placeholders (Codex, PR #26).
    emit("decision", "Costs compared", stage="plan", verdicts=[asdict(v) for v in result.verdicts],
         inputs=result.inputs, scenarios=result.scenarios)
    placeholder = any(i.get("status") == "placeholder" for i in result.inputs.values())
    current = next((r for r in result.routes if r.key == "current"), None)
    whole = current is not None and current.complete
    # A route that can't make the whole part has no break-even quantity: no payback is claimed (Codex, PR #26).
    return ({r.key: {"one_time": list(r.one_time), "costs": {str(q): list(v) for q, v in r.costs.items()},
                    "payback": list(r.payback) if r.payback is not None and whole and r.complete else None,
                    "payback_mid": r.payback_mid if whole and r.complete else None,
                    "complete": r.complete, "unpriced": r.unpriced, "placeholder": placeholder}
            for r in result.routes if r.key in options}, result)


def _rank(result, shop: ShopProfile, issues: list[Issue], options: dict[str, Option], emit: Emit,
          intents: dict[str, Intent] | None = None) -> tuple[dict, dict]:
    """ADR 0010: per issue, its plan-changing fixes ranked by cost (dominance at every corner and quantity, then
    the mid-range tie-break Rayyan chose, 2026-10-30). Each issue over its own fixes, so a cutter for one face
    never leads for another. Shop-side fixes always; once the designer has freed the face, its part edits too,
    with their own edit time waived (Amendment 1, Rayyan 2026-10-31), unless the designer explicitly asked for
    one side. A ranking bug costs only the ranking, never the review. Returns (rankings, keys ranked) per issue."""
    if result is None:
        return {}, {}
    from anville.shop import rank_fixes
    from anville.shop.cost import DESIGNER_TIME

    out, considered = {}, {}
    for issue in issues:
        intent = (intents or {}).get(issue.face_group)
        freed = intent is not None and intent.status == "free"
        prefer = intent.prefer if freed else ""
        keys = [k for k, o in options.items()  # best_option order: `top` keeps it, the fallback relies on it
                if o.changes_plan and (
                    (o.side == "shop" and o.face_group in (None, issue.face_group) and prefer != "part_edit")
                    or (freed and o.side == "part" and o.face_group == issue.face_group and prefer != "shop_fix"))]
        if not keys:
            continue
        try:
            ranking = rank_fixes(result, shop, keys, waived={DESIGNER_TIME} if freed else set())
        except Exception as e:
            emit("error", "Ranking failed; any shop fix may lead", stage="plan", issue_id=issue.id, error=repr(e)[:300])
            continue
        out[issue.id], considered[issue.id] = ranking, keys
        emit("decision", "Fixes ranked", stage="plan", issue_id=issue.id, top=list(ranking.top), lead=ranking.lead,
             basis=ranking.basis, freed=freed, prefer=prefer or None)
    return out, considered


_COMPARES = re.compile(r"\bat some (inputs|shop rates|rates)\b", re.I)  # the rival's side of the ranking
_RIVAL_FIRST = re.compile(r"\{\{\s*option\.(.+?)\.fix\s*\}\}")  # match(): the rival's name opens the sentence
_PAYS = re.compile(r"\bpays? (off|back)\b|\bpayback\b", re.I)


def payback_covered(key: str, payback: tuple, mid: int | None, sentences: list[str]) -> bool:
    """Verified prose already stated the payback: every end of the range pays off (a "never" is code's to say),
    and one sentence says it pays off with all three slots, since the range is what says it depends on inputs
    (ADR 0010 §5)."""
    if None in (*payback, mid):
        return False
    slots = {f"option.{key}.payback_{f}" for f in ("lo", "mid", "hi")}
    return any(_PAYS.search(t) and slots <= {m.strip() for m in _SLOT.findall(t)} for t in sentences)


def payback_line(key: str, payback: tuple, mid: int | None) -> str:
    """When buying the tool pays off, as a slot template. The verdict stays "depends": an end of the range
    that never pays off is said so, never filled in (shop lane, 2026-10-29)."""
    lo, hi = payback
    tool = f"{{{{option.{key}.tool}}}}"
    if lo is None:
        return f"Buying the {tool} doesn't pay off at these inputs."
    if mid is None:
        return (f"Buying the {tool} pays off past {{{{option.{key}.payback_lo}}}} parts at the best inputs, "
                "but never at mid-range ones.")
    span = (f"anywhere from {{{{option.{key}.payback_lo}}}} to {{{{option.{key}.payback_hi}}}}" if hi is not None
            else f"from {{{{option.{key}.payback_lo}}}} at best; at the worst inputs it never does")
    return (f"Buying the {tool} pays off past ~{{{{option.{key}.payback_mid}}}} parts at mid-range inputs "
            f"({span}); below that, keep the flip.")


# --- fixed-script steps ------------------------------------------------------------


def _intent_question(group: str) -> str:
    return f"Is {group} required as modelled?"


def _with_intent_questions(decisions: list[Decision], by_id: dict[str, Issue], intents: dict[str, Intent],
                           gates: dict[str, bool]) -> list[Decision]:
    """Rayyan's ruling (a), 2026-10-30: where a face's purpose is unknown and the answer could change the plan,
    the proposal always carries the intent question, so the comment invites the reply the re-plan needs (the model
    asked in 1 of 20 live-path runs). A question the model wrote and the checks passed is kept. One question per
    fix: a part-wide fix covering several issues asks once."""
    out, asked = [], set()
    for d in decisions:
        g = by_id[d.issue_id].face_group
        open_question = d.action == "propose" and gates.get(d.issue_id) and intents[g].status == "unknown"
        if d.question and d.action == "propose" and d.option_key in asked:
            d = replace(d, question=None)  # already asked about this fix
        elif open_question and not d.question and d.option_key not in asked:
            d = replace(d, question=f"Is {g} required as modelled?")
        if d.question and d.action == "propose":
            asked.add(d.option_key)
        out.append(d)
    return out


def _fallback_decision(issue: Issue, may_ask: bool, options: list[Option], ranking=None,
                       considered=(), prefer: str = "") -> Decision:
    unwanted = {"shop_fix": "part", "part_edit": "shop"}.get(prefer)  # the designer said which side, explicitly
    mine = [o for o in options if o.face_group in (None, issue.face_group)]
    if any(o.changes_plan and o.side != unwanted for o in mine):
        mine = [o for o in mine if o.side != unwanted]  # only if the side they asked for has a fix (Codex, PR #41)
    if ranking and ranking.top:  # ADR 0010: the ranked lead, else the first of the top tier, leads among the ranked
        first = ranking.lead or ranking.top[0]
        mine = sorted(mine, key=lambda o: o.key in considered and o.key != first)
    if may_ask:
        # Intent unknown: propose the fix that assumes nothing about the feature (shop side, when one
        # exists) and ask about the part edit the answer could unlock. Never ask alone.
        question = f"Is {issue.face_group} required as modelled?"
        shop_fix = best_option(mine, part_edits=False)
        if shop_fix:
            return Decision(issue.id, "propose", option_key=shop_fix.key, question=question,
                            reason="its intent is unknown: the proposal assumes nothing about it, and the "
                                   "answer could unlock a part edit")
        if best_option(mine, part_edits=True):  # only a part edit helps: flag and ask (ruling, 2026-10-30)
            return Decision(issue.id, "ask", question=question,
                            reason="its intent is unknown and only a part edit fixes it")
    best = best_option(mine, part_edits=True)
    if best:
        return Decision(issue.id, "propose", option_key=best.key, reason="the best plan-changing option offered")
    return Decision(issue.id, "quiet", reason="no option changes the plan")


def render_line(d: Decision, by_id: dict[str, Issue], intents: dict[str, Intent], options: dict[str, Option]) -> str:
    """One decision as a slot template written by code: the fallback for any unverified statement."""
    g = by_id[d.issue_id].face_group
    intent = intents.get(g)
    if d.action == "ask":
        return f"{g}: only a part edit fixes this, and its purpose isn't on record."
    if d.action == "propose":
        o = options[d.option_key]
        name = _name(o)
        line = f"{g}: {name[0].upper()}{name[1:]} {_effect(o)}."
        if o.side == "shop":
            line += " It changes the shop, not the part."
        if intent and intent.status == "protected":
            # A keep constraint may carry no reason; its slot then doesn't exist (Greptile, PR #14).
            line += f" {g} stays as designed: {{{{intent.{g}.reason}}}}." if intent.reason else f" {g} stays as designed."
        return line
    return f"{g}: no change proposed."


def _fix(key: str, o: Option) -> str:
    """_name with its tool slot filled: "buying the 910480 back-chamfer cutter"."""
    return _name(o).replace(f"{{{{option.{key}.tool}}}}", str(o.params.get("name", key)))


def _name(o: Option) -> str:
    """An option in the designer's words, as a slot template: option keys are for the trace, not the
    comment (shop lane's demo check of #26)."""
    g, p = o.face_group, o.params
    if o.kind == "remove_feature":
        return f"removing {g}"
    if o.kind == "set_depth":
        return f"changing {g}'s depth"
    if o.kind == "set_min_radius":
        return f"opening up {g}'s inside corner radius"
    if o.kind == "route_machine":
        return "sending it to an outside 5-axis shop"
    if o.kind == "add_fixture":  # not offered yet (edits.py skips it), named so a new kind can't leak a key
        return "a different fixture"
    if o.kind == "add_tool":
        tool = f"{{{{option.{o.key}.tool}}}}"
        if p.get("type") == "back_chamfer":
            return f"{'using the shop' + chr(39) + 's' if p.get('owned') else 'buying the'} {tool} back-chamfer cutter"
        if p.get("type") == "holder":
            return f"running the {tool} in a slimmer holder"
        return f"buying a longer-reach {tool}"
    return o.key


def _effect(o: Option, *, short: bool = False) -> str:
    """What an option does, as a slot template: unreachable faces when it changes those, else setups (eval
    gate, PR #23: an unreachable bore read "takes it from 1 to 1 setup(s)"). `short` drops the "from"."""
    if o.before.unreachable == o.after.unreachable and o.before.setups == o.after.setups \
            and getattr(o, "tool_changes", None):  # Codex, PR #23: a tool-only change isn't "from 1 to 1"
        return f"changes the tool that cuts it and keeps {{{{option.{o.key}.setups_after}}}} setup(s)"
    name, unit = ("unreachable", "unreachable face(s)") if o.before.unreachable != o.after.unreachable \
        else ("setups", "setup(s)")
    start = "" if short else f"from {{{{option.{o.key}.{name}_before}}}} "
    return f"takes it {start}to {{{{option.{o.key}.{name}_after}}}} {unit}"


def _result_slot(o: Option) -> str:
    """The slot an action sentence must state for this proposal's result."""
    name = "unreachable_after" if o.before.unreachable != o.after.unreachable else "setups_after"
    return f"{{{{option.{o.key}.{name}}}}}"


def _group_order(label: str) -> int:
    return int(label[1:])
