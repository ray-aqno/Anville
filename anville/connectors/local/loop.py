"""The design loop on a LocalSource and LocalSink: review, reply, shop note.

Each turn is modal_app's (_review, _replan, _on_shop_note) with the Onshape calls swapped for the source and the
sink. Every decision is job.py's, so a local review says what the Onshape one would; what can't happen here is
the fix branch (no CAD edit API), which becomes the written fix ADR 0001 falls back to.
"""

from __future__ import annotations

import uuid
from typing import Mapping

from anville.agent.job import (
    agent_comment, analyze_step, flushing_at_stage_changes, read_reply_turn, removal_alternative, review_shop,
    setup_count_message, sheet_for, sheet_id_for, shop_note_writes, with_credits,
)
from anville.agent.model import ChatModel
from anville.agent.reply import constraint_entry, constraints_from_ledger, nothing_read, unrecorded
from anville.connectors import Part
from anville.connectors.local import LocalError, LocalSink, review_key

__all__ = ["LocalError", "reply", "review", "shop_note", "written_fix"]


def review(part: Part, sink: LocalSink, env: Mapping[str, str], *, model: ChatModel | None = None,
           verifier: ChatModel | None = None) -> str:
    """Review this version: the comment, the setup sheet and the trace. Returns the comment."""
    n = sink.runs(part) + 1
    key = review_key(part, n)
    sink.store.record(key, part.document_id, {"versionId": part.version_id, "name": part.name},
                      version_id=part.version_id)
    trace = sink.trace(part, f"{part.version_id[:8]}-{n}")
    trace.add("run_started", "Reviewing a new version")
    trace.flush()  # before the slow extraction, so a replay shows the run in progress (Codex P2)
    try:
        message = _review(part, sink, env, trace, key, model=model, verifier=verifier)
    except Exception as e:  # close the run, so a replay shows it failed rather than stuck
        trace.add("error", "Review failed", error=type(e).__name__, message=str(e)[:300])
        trace.add("run_finished", "Review ended with an error")
        trace.flush()
        raise
    return message


def _review(part, sink, env, trace, key, *, model, verifier) -> str:
    ledger = sink.shop_ledger()
    result = analyze_step(part.step, ledger)
    plan = result["plan"]
    trace.add("verify", "Setup count computed from geometry", setups=plan["count"],
              directions=[s["directions"] for s in plan["setups"]],
              unreachable=len(plan["unreachable"]), faces=result["faces"], extract_s=result["extract_s"])
    shop, learned = review_shop(ledger)
    if learned:
        trace.add("tool_result", "Used what the shop learned", tools=learned)
    comment, stats = agent_comment(result["facts"], shop, env, flushing_at_stage_changes(trace.add, trace.flush),
                                   back_chamfer=result["back_chamfer"], learned=learned, model=model,
                                   verifier=verifier)
    trace.flush()
    message = comment or setup_count_message(part.version_id, plan)
    link = _store_sheet(part, sink, trace, key, result, ledger, stats.get("buying") or [])
    if link:
        message += f"\nSetup sheet for the shop: {link}"
    fix = written_fix(result["facts"], shop, result["back_chamfer"])
    if fix:
        message += f"\n{fix}"
    if comment:  # the credits line stays last, as on Onshape
        message = with_credits(message, stats.get("agent_usage", []), stats.get("agent_fallbacks", []))
    sink.store.reserve_post(key, "review")  # the record is_own reads: the self-loop guard (RFC 0006)
    cid = sink.post_review(part, message)
    sink.store.complete_post(key, "review", cid)
    trace.add("comment_posted", "Posted the review", comment_id=cid, posted_as="local")
    trace.add("run_finished", "Review finished")
    trace.flush()
    return message


def reply(part: Part, sink: LocalSink, env: Mapping[str, str], text: str, *, reader: ChatModel | None = None,
          model: ChatModel | None = None, verifier: ChatModel | None = None) -> str | None:
    """A designer's reply to this version's review (ADR 0012): its rules join the constraint ledger and the version
    is re-planned, answered in the thread. Returns the answer, or None when the reply decides nothing."""
    review_event = sink.last_review(part)
    if review_event is None:
        raise LocalError(f"{part.name} has no review of this version yet: review it first")
    n = sink.runs(part, replies=True) + 1
    reply_id = f"reply:{part.version_id}#{n}"
    trace = sink.trace(part, f"{part.version_id[:8]}-r{n}")
    trace.add("run_started", "Reading a designer's reply", comment_id=reply_id)
    trace.flush()
    try:
        message = _replan(part, sink, env, trace, text, reply_id, review_event, reader=reader, model=model,
                          verifier=verifier)
    except Exception as e:
        trace.add("error", "Re-plan failed", error=type(e).__name__, message=str(e)[:300])
        trace.add("run_finished", "Re-plan ended with an error")
        trace.flush()
        raise
    return message


def _replan(part, sink, env, trace, text, reply_id, review_event, *, reader, model, verifier) -> str | None:
    ledger = sink.shop_ledger()
    result = analyze_step(part.step, ledger)
    shop, learned = review_shop(ledger)
    then, _ = review_shop([r for r in ledger if r.get("created_at", 0) <= review_event.received_at])
    emit = flushing_at_stage_changes(trace.add, trace.flush)
    found, groups, setups, (read_calls, read_fallbacks) = read_reply_turn(
        result["facts"], shop, env, emit, text, model=reader, back_chamfer=result["back_chamfer"], issue_shop=then)
    scope = f"doc:{part.document_id}"
    entries = [constraint_entry(rule, reply_id=reply_id, review_key=review_event.key, version_id=part.version_id)
               for rule in found or []]
    for entry in unrecorded(entries, sink.store.read_ledger("constraint", scope)):
        sink.store.append_ledger("constraint", scope, entry["payload"], entry["provenance"])
    for rule in found or []:
        trace.add("tool_result", "Constraint recorded", face_group=rule["face_group"], rule=rule["rule"],
                  quote=rule["quote"])
    if found:
        constraints = constraints_from_ledger(sink.store.read_ledger("constraint", scope),
                                              version_id=part.version_id)
        comment, stats = agent_comment(result["facts"], shop, env, emit, back_chamfer=result["back_chamfer"],
                                       learned=learned, constraints=constraints, model=model, verifier=verifier)
        body = comment or setup_count_message(part.version_id, result["plan"])
        # Its own sheet only when the sheet can show every proposal (back-chamfer cutters), as on Onshape.
        link = _store_sheet(part, sink, trace, f"comment:{reply_id}", result, ledger, stats.get("buying") or []) \
            if stats.get("sheet_ok", False) else None
        if link:
            body += f"\nSetup sheet for the shop: {link}"
        message = with_credits(body, read_calls + stats.get("agent_usage", []),
                               [*read_fallbacks, *stats.get("agent_fallbacks", [])])
    else:
        message = with_credits(nothing_read(groups, setups), read_calls, read_fallbacks) if groups else None
    trace.flush()
    if message:
        sink.store.reserve_post(reply_id, "replan")
    cid = sink.post_reply(part, reply_id, text, message)
    if cid:
        sink.store.complete_post(reply_id, "replan", cid)
        trace.add("comment_posted", "Answered in the thread", comment_id=cid, posted_as="local")
    trace.add("run_finished", "Re-plan finished")
    trace.flush()
    return message


def shop_note(part: Part, sink: LocalSink, *, arrived: list[str] = (), outcome: str = "as_planned",
              text: str = "", setups_run: int | None = None, sheet: str | None = None) -> list[dict]:
    """The machinist's note on this part's setup sheet (the latest, or the review's `sheet` it answers): into the
    shop's ledger, so the next review starts from it (a cutter ticked as arrived is one the shop owns). Returns the
    entries written."""
    sheet = sink.read_sheet(part, sheet)
    if sheet.get("shop") != sink.shop_scope:  # the wrong shop would learn what another shop's run did (Codex P2)
        raise LocalError(f"{part.name}'s setup sheet was made for shop {sheet.get('shop')!r}, not "
                         f"{sink.shop_scope!r}: note it under that shop, or review the part for this one first")
    note = {"note_id": f"local-{uuid.uuid4().hex[:12]}", "sheet_id": sheet["sheet_id"], "outcome": outcome,
            "tools_arrived": list(arrived), "tags": [], "setups_run": setups_run, "note_text": text}
    entries = shop_note_writes(note, sheet, stored=sink.shop_ledger(), sheet_document=part.document_id,
                               note_document=part.document_id)
    for entry in entries:
        sink.store.append_ledger("shop_outcome", sink.shop_scope, entry["payload"], entry["provenance"])
    return entries


def written_fix(facts: dict, shop, back_chamfer: list[dict] = ()) -> str | None:
    """The removal alternative as words, where Onshape would build it on a branch and measure it (ADR 0001: a
    CAD edit that can't be made falls back to its intent, written). None when no removal lowers setups."""
    from anville.geometry import PartFacts, describe_group

    option = removal_alternative(facts, shop, back_chamfer)
    if option is None:
        return None
    where = describe_group(PartFacts.from_dict(facts), option.face_group)
    subject = f"{where} ({option.face_group})" if where else option.face_group
    return (f"Written fix (no CAD edit here, so no branch to measure): removing {subject} would take it to "
            f"{option.after.setups} setup(s), predicted from the geometry.")


def _store_sheet(part, sink, trace, key, result, ledger, buying) -> str | None:
    """The setup sheet for the shop, as modal_app._store_sheet: a sheet failure costs only the sheet."""
    try:
        shop, learned = review_shop(ledger)
        sheet = sheet_for(result["facts"], shop, result["back_chamfer"], buying=buying, provenance=learned)
        sid = sheet_id_for(key)
        link = sink.store_sheet(part, sid, sheet)
    except Exception as e:
        trace.add("error", "Setup sheet failed", error=repr(e)[:300])
        return None
    trace.add("tool_result", "Setup sheet ready", sheet_id=sid, setups=len(sheet["setups"]), buying=buying,
              sheet_url=None)  # no signed link offline: the sheet is the file the comment names
    return link
