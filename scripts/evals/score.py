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


def no_bare_question(result) -> bool:
    """Flag, then suggest: a question only ever rides on a proposal."""
    return all(d.action != "ask" and (not d.question or d.action == "propose") for d in result.decisions)


def numbers_from_code(result, values: dict[str, str]) -> bool:
    """Every number in the comment is one code computed (locked decision 3)."""
    allowed = {n for v in values.values() for n in _NUMBER.findall(v)}
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
    """Stated intent, or a designer's "keep it": the proposal changes the shop, never the part."""
    return side_of(result, group) == "shop"


def any_intent_leads(result, group: str) -> bool | None:
    """Unknown intent: the proposal is a fix that works whatever the face is for (shop side). None when
    code offers no plan-changing shop fix for this face (the ruling doesn't cover that case)."""
    if not any(o.side == "shop" and o.changes_plan and o.face_group in (None, group)
               for o in result.options.values()):
        return None
    return shop_fix(result, group)


def removal_conditional_only(result, group: str) -> bool:
    """Unknown intent: a part edit is named only in code's conditional line, next to the question."""
    d = decision_for(result, group)
    part_keys = [k for k, o in result.options.items() if o.side == "part" and o.face_group == group]
    for line in result.comment.splitlines():
        if any(k in line for k in part_keys):
            if not (line.startswith(f"If {group} isn't needed,") and d is not None and d.question):
                return False
    return True


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
