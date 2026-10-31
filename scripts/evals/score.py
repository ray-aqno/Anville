"""Scoring for the multi-turn eval gate: each check reads one ReviewResult, in code, never by a model.

The checks follow the council ruling Rayyan accepted on 2026-10-29 (flag, then suggest), with his patch:
code enforces most of the ruling before a comment posts, so the gate also scores whether the model's
own decision survived. A comment the fallback wrote passes the ruling but not survival.

A check returns True or False, or None when it doesn't apply to that run (it then counts for nothing).
"""

from __future__ import annotations

import re

# Words in a comment that carry digits but aren't numbers: option keys, group labels, axes, directions.
_LABELS = re.compile(r"\b[GI]\d+\b|\b[35]-axis\b|[+-][XYZ]\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
MODEL_STAGES = ("plan", "decide")  # a fallback here means code made the decision, not the model


def decision_for(result, group: str):
    """The decision on the issue at `group`, or None if that issue wasn't selected or decided."""
    ids = {i.id for i in result.issues if i.face_group == group}
    return next((d for d in result.decisions if d.issue_id in ids), None)


def survived(result) -> bool:
    """Rayyan's patch: the model's own decision reached the comment (no Plan or Decide fallback)."""
    return not set(result.fallbacks) & set(MODEL_STAGES)


def ask_only(result) -> set[str]:
    """Issues where asking alone is right (Rayyan's ruling, 2026-10-30, lane 1): intent unknown, the gate
    allows a question, no shop-side fix changes the plan, and a part edit does."""
    out = set()
    for i in result.issues:
        if i.id not in getattr(result, "selected", ()):
            continue
        mine = [o for o in result.options.values() if o.face_group in (None, i.face_group) and o.changes_plan]
        if (getattr(result, "intents", {}).get(i.face_group) is not None
                and result.intents[i.face_group].status == "unknown" and result.ask_allowed.get(i.id)
                and not any(o.side == "shop" for o in mine) and any(o.side == "part" for o in mine)):
            out.add(i.id)
    return out


def no_bare_question(result) -> bool:
    """Flag, then suggest: a question rides on a proposal, except where only removal would help (lane 1)."""
    allowed = ask_only(result)
    return all((d.action != "ask" or d.issue_id in allowed) and (not d.question or d.action in ("propose", "ask"))
               for d in result.decisions)


def asks_without_removal(result) -> bool | None:
    """Where only removal helps: the review asks about the feature, and never proposes removing it.
    None when the run has no such issue."""
    only = ask_only(result)
    if not only:
        return None
    mine = [d for d in result.decisions if d.issue_id in only]
    leads = any(d.action == "propose" and result.options[d.option_key].side == "part" for d in mine)
    return not leads and any(d.action == "ask" and d.question for d in mine)


def flags_and_asks(result) -> bool:
    """For a part where only removal can help (the scenario says so; the check doesn't trust the run's own
    options): at least one issue is flagged with a question. Quiet everywhere fails, whatever was scored."""
    return any(d.action in ("ask", "propose") and d.question for d in result.decisions)


def estimate_shown(result, values: dict[str, str]) -> bool | None:
    """Lane 3: where only removal helps and code priced it, the dollars reach the comment.
    None when nothing was priced (a shop without cost inputs)."""
    lows = [v for k, v in values.items() if k.endswith(".cost_lo")]
    if not ask_only(result) or not lows:
        return None
    return any(f"${v}" in result.comment for v in lows)


def numbers_from_code(result, values: dict[str, str]) -> bool:
    """Every number in the comment is one code computed (locked decision 3). Numbers inside option keys
    are code's too: a rendered fix names its catalog cutter ("the 910480 back-chamfer cutter")."""
    keys = [_LABELS.sub("", k) for k in result.options]  # "remove_feature/G3" must not allow a bare 3
    allowed = {n for v in [*values.values(), *keys] for n in _NUMBER.findall(v)}
    text = result.comment
    for key in sorted(result.options, key=len, reverse=True):
        text = text.replace(key, "")
    return set(_NUMBER.findall(_LABELS.sub("", text))) <= allowed


def raised(result, group: str) -> bool:
    """The issue at `group` was selected and a fix proposed for it (not dropped, not quiet)."""
    d = decision_for(result, group)
    return d is not None and d.action == "propose"


def side_of(result, group: str) -> str | None:
    d = decision_for(result, group)
    return result.options[d.option_key].side if d and d.option_key in result.options else None


def shop_fix(result, group: str) -> bool:
    """Stated intent, or a designer's "keep it": the proposal changes the shop, never the part, and it
    changes the plan (live, 2026-10-30: a no-op 5-axis route was proposed on the undercut part)."""
    d = decision_for(result, group)
    return side_of(result, group) == "shop" and result.options[d.option_key].changes_plan


def ranked_lead(result, group: str, ranked: dict[str, str]) -> bool | None:
    """ADR 0010: a shop-side proposal is the cost-ranked fix code chose (`ranked`: issue id -> lead, from
    the "Fixes ranked" trace event). None when the code under test doesn't rank this issue, or the lead is
    a part edit (the ranking orders shop fixes; after "not needed", removal may lead)."""
    d = decision_for(result, group)
    if d is None or d.issue_id not in ranked or side_of(result, group) != "shop":  # dropped: `raised` fails
        return None
    return d.option_key == ranked[d.issue_id]


def any_intent_leads(result, group: str) -> bool | None:
    """Unknown intent: the proposal is a fix that works whatever the face is for (shop side). None when
    code offers no plan-changing shop fix for this face (the ruling doesn't cover that case)."""
    if not any(o.side == "shop" and o.changes_plan and o.face_group in (None, group)
               for o in result.options.values()):
        return None
    return shop_fix(result, group)


def intent_asked(result, group: str) -> bool | None:
    """Rayyan's ruling (a), 2026-10-30: intent unknown and the ask gate open, so the comment asks what the
    face is for (the designer's reply drives the re-plan). None when intent is known or asking isn't allowed."""
    d = decision_for(result, group)
    intent = getattr(result, "intents", {}).get(group)
    if d is None or intent is None or intent.status != "unknown" or not result.ask_allowed.get(d.issue_id):
        return None
    return bool(d.question)


def removal_conditional_only(result, group: str) -> bool:
    """Unknown intent: a part edit never leads, and code's conditional line ("If G3 isn't needed, ...")
    appears at most once, only next to the question. Judged from the decision: rendered lines name
    fixes in words, not option keys (main, 2026-10-30). Model prose naming a part edit is rejected by
    review.py's sentence check, which this doesn't repeat."""
    d = decision_for(result, group)
    if d is not None and d.option_key and result.options[d.option_key].side == "part":
        return False
    conditional = [ln for ln in result.comment.splitlines() if ln.startswith(f"If {group} isn't needed,")]
    return len(conditional) <= 1 and (not conditional or (d is not None and bool(d.question)))


def plan_changing_fix(result, group: str) -> bool:
    """A designer's "not needed": any proposal that changes the plan respects it."""
    d = decision_for(result, group)
    return d is not None and d.action == "propose" and result.options[d.option_key].changes_plan


def nothing_raised(result) -> bool:
    """Part B with the cutter owned: nothing to raise, so no model call is made."""
    return not result.issues and result.model_calls == 0


def repeated_fixes(result) -> int:
    """Proposals that repeat an option already proposed for another issue in the same comment."""
    keys = [d.option_key for d in result.decisions if d.action == "propose"]
    return len(keys) - len(set(keys))


def question_names_no_fix(question: str | None, catalog: tuple[str, ...], group: str) -> bool | None:
    """A rough code check that the question is about intent: it names the face and no edit or fix.
    (Live, 2026-10-30: one question suggested "adjusting its orientation", which no option offers.)"""
    if not question:
        return None
    words = question.lower()
    return group in question and not any(k.replace("_", " ") in words or k in words for k in catalog)


# Lines code adds to every comment by design, not review content: they count in neither total.
_NOT_REVIEW = ("Setup sheet for the shop:", "Reviewed by ", "Reply read by ")


def model_line_share(comment: str, model_lines: int) -> tuple[int, int]:
    """The prose metric (Rayyan, 2026-10-31: "constrain the accuracy, not the wording"): (lines the model wrote,
    review lines). `model_lines` comes from the trace ("Comment assembled" carries model_sentences)."""
    lines = [ln for ln in comment.splitlines() if ln.strip() and not ln.startswith(_NOT_REVIEW)]
    return model_lines, len(lines)
