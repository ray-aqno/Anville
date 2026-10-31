"""The agent inside the deployed review job (ADR 0009). Kept free of Modal so it runs in tests.

The job always has a fallback: the plain setup-count line, which code renders from the SetupPlan.
So the agent returns None whenever it is switched off or breaks, and a review is never lost to an
agent bug.
"""

from __future__ import annotations

from typing import Mapping

from anville.agent.model import ChatModel, OpenAICompatModel
from anville.agent.review import Emit, run_review
from anville.geometry import PartFacts, ShopProfile
from anville.geometry.access import BackChamferAccess


# The demo shop's ledger scope (the shop lane writes its tool_acquired rows here).
SHOP_SCOPE = "shop:dll"
# Placeholder end mills until the shop-profile format is decided (CLAUDE.md §4); same as the geometry tests.
DEFAULT_TOOLS = (("EM12", 12.0, 4.0), ("EM6", 6.0, 4.0), ("EM3", 3.0, 4.0))


def review_shop(ledger: list[dict]) -> tuple[ShopProfile, dict[str, dict]]:
    """The shop a review plans and prices for: Marquette's DLL with its cost inputs, relieved soft jaws
    (RFC 0002 §3), and every cutter the ledger says it bought since (S8). Returns the provenance of
    each learned tool, by name, for the trace: "it knew the shop has that cutter now"."""
    from anville.geometry import SoftJaws, Tool
    from anville.shop import DLL_COSTS, Shop, apply_ledger

    base = Shop(tools=[Tool(*t) for t in DEFAULT_TOOLS], fixture=SoftJaws(), name="Marquette DLL", costs=DLL_COSTS)
    # The store doesn't validate payloads: a row that can't be applied costs only itself (Codex, PR #26).
    usable = []
    for row in ledger:
        try:
            apply_ledger(base, [row])
        except Exception:
            continue
        usable.append(row)
    return apply_ledger(base, usable)


def catalog_for(shop: ShopProfile) -> list:
    """The catalog cutters the shop doesn't own yet: assessed as purchases to offer."""
    from anville.geometry import BACK_CHAMFER_CATALOG

    owned = {t.name for t in shop.back_chamfer_tools}
    return [c for c in BACK_CHAMFER_CATALOG if c.name not in owned]


def flushing_at_stage_changes(add: Emit, flush) -> Emit:
    """`add` that flushes right after an event that opens a new stage. The opening event ("Model
    called") comes before the stage's slow call, so the panel sees each stage start live, and the
    previous stage's events go out with it (ADR 0007; Codex, PR #24). Events without a stage are not
    boundaries."""
    last = None

    def emit(kind: str, title: str, **detail) -> None:
        nonlocal last
        stage = detail.get("stage")
        add(kind, title, **detail)
        if stage is not None and stage != last:
            last = stage
            flush()
    return emit


def agent_comment(facts: dict, shop: ShopProfile, env: Mapping[str, str], emit: Emit, *,
                  model: ChatModel | None = None, verifier: ChatModel | None = None,
                  deadline: float | None = None, back_chamfer: list[dict] = (),
                  learned: dict[str, dict] | None = None,
                  constraints: list[dict] | None = None) -> tuple[str | None, dict]:
    """The agent's comment and its run stats for the job's timing log.

    `ANVILLE_AGENT=off` skips it. Models come from `env` unless passed in; the verifier
    (muse-spark-1.3 on the Meta Model API) is used only when its key is set. `deadline`
    (`time.monotonic()`) bounds every model call, so the job keeps time to post. `back_chamfer` is
    analyze()'s cutter assessments, as dicts. `learned` is review_shop()'s provenance: a cutter the shop
    bought and this plan uses is named in the comment (S8: Part B arrives already informed).
    """
    if env.get("ANVILLE_AGENT", "on") != "on":
        return None, {"agent": "off"}
    try:
        model = model or OpenAICompatModel.from_env(env, "open")
        if verifier is None and env.get("MODEL_API_KEY"):
            verifier = OpenAICompatModel.from_env(env, "spark")
        r = run_review(PartFacts.from_dict(facts), shop, model, verifier=verifier, emit=emit,
                       deadline=deadline, back_chamfer=[BackChamferAccess.from_dict(a) for a in back_chamfer],
                       constraints=constraints)
    except Exception as e:  # an agent bug must not cost the designer the review
        error = repr(e)[:300]
        emit("error", "Agent failed; posting the setup count instead", error=error)
        return None, {"agent_error": error}
    # One record per call (stage, model, host, tokens): the panel shows which host served what.
    emit("verify", "Agent finished", stage="publish", calls=r.usage, fallbacks=r.fallbacks)
    comment = r.comment
    used = {a.tool for s in r.plan.setups for a in s.faces}
    for name, source in sorted((learned or {}).items()):
        if comment and name in used:  # only what this plan relies on: the learning, made visible
            on = f" on {source['on']}" if source.get("on") else ""
            comment += f"\nIt plans with the {name} the shop bought{on}."
    return comment or None, {"agent_calls": r.model_calls, "agent_fallbacks": r.fallbacks,
                               "agent_usage": r.usage, "buying": buying_from(r)}


def buying_from(r) -> list[str]:
    """The catalog back-chamfer cutters the review proposed buying: the setup sheet plans with them, unowned, so
    the traveler can tick them as arrived (demo gap 2)."""
    names = []
    for d in r.decisions:
        o = r.options.get(d.option_key) if d.action == "propose" else None
        if o and o.kind == "add_tool" and o.params.get("type") == "back_chamfer" and o.params.get("owned") is False:
            names.append(o.params["name"])
    return sorted(set(names))


def sheet_for(facts: dict, shop: ShopProfile, back_chamfer: list[dict], *, buying: list[str] = (),
              provenance: dict[str, dict] | None = None) -> dict:
    """The setup sheet the shop runs: the plan with the cutters the review decided to buy (demo gap 3)."""
    from dataclasses import replace

    from anville.geometry import BACK_CHAMFER_CATALOG, min_setups
    from anville.shop.sheet import setup_sheet

    part = PartFacts.from_dict(facts)
    access = [BackChamferAccess.from_dict(a) for a in back_chamfer]
    catalog = {c.name: c for c in BACK_CHAMFER_CATALOG}
    with_bought = replace(shop, back_chamfer_tools=[*shop.back_chamfer_tools, *(catalog[n] for n in buying)])
    plan = min_setups(part, with_bought, back_chamfer=access)
    return setup_sheet(part, shop, plan, back_chamfer=access, provenance=provenance, buying=tuple(buying))


def shop_note_writes(note: dict, sheet: dict, *, stored: list[dict], sheet_document: str | None = None,
                     note_document: str | None = None) -> list[dict]:
    """A shop note's ledger entries not already written, so a retried job writes nothing twice and finishes what a
    job that died mid-write began. A note may answer only a sheet on its own document, and a note id already
    recorded can't come back saying something else (Codex, PR #40)."""
    from anville.shop.ledger import SHOP_NOTE, shop_note_entries

    if sheet_document != note_document:
        raise ValueError("a shop note may only answer a sheet stored on its own document")
    entries = shop_note_entries(note, sheet=sheet)
    decision = entries[0]["provenance"]["decision"]
    before = [r for r in stored if r["provenance"].get("decision") == decision]
    said = next((r["payload"] for r in before if r["payload"].get("kind") == SHOP_NOTE), None)
    if said is not None and said != entries[0]["payload"]:
        raise ValueError(f"{decision} is already recorded, saying something else")
    key = lambda e: (e["payload"].get("kind"), e["payload"].get("name"))  # noqa: E731
    seen, out = {key(r) for r in before}, []
    for e in entries:  # a cutter ticked twice is acquired once
        if key(e) not in seen:
            seen.add(key(e))
            out.append(e)
    return out


def sheet_id_for(event_key: str) -> str:
    """The sheet id for a review event: the same on a retry, so a retried review can't announce a second sheet
    (Codex, PR #40). The link's HMAC is the capability, so the id needn't be secret."""
    import hashlib

    return hashlib.sha256(event_key.encode()).hexdigest()[:32]


def read_reply_turn(facts: dict, shop: ShopProfile, env: Mapping[str, str], emit: Emit, reply: str, *,
                    model: ChatModel | None = None, deadline: float | None = None,
                    back_chamfer: list[dict] = (),
                    issue_shop: ShopProfile | None = None) -> tuple[list[dict] | None, list[str], int]:
    """ADR 0012, first half of a reply turn: (rules, the issue groups a reply may decide, setups now).

    The groups are the issues code finds on this version, so a rule can only be about a face the review could
    have raised. `rules` is None when the agent is off or the read fell back; the job then answers with
    `nothing_read`, never a guessed rule. `issue_shop`: the shop as it was when the review ran, so a cutter
    bought since doesn't hide the issue the designer is answering (Codex, PR #38). `setups` is today's.
    """
    from anville.agent.reply import read_reply
    from anville.agent.review import find_issues
    from anville.geometry import min_setups

    part = PartFacts.from_dict(facts)
    access = [BackChamferAccess.from_dict(a) for a in back_chamfer]
    plan = min_setups(part, shop, back_chamfer=access)
    then = plan if issue_shop is None else min_setups(part, issue_shop, back_chamfer=access)
    issues = find_issues(part, then)
    raised = {i.face_group: i.kind for i in issues}
    groups = list(dict.fromkeys(i.face_group for i in issues))
    if env.get("ANVILLE_AGENT", "on") != "on":
        return None, groups, plan.count
    try:
        model = model or OpenAICompatModel.from_env(env, "open")
        found, _ = read_reply(model, reply, raised, emit=emit, deadline=deadline)
    except Exception as e:  # a reader bug must not cost the designer an answer
        emit("error", "Reading the reply failed", stage="read_reply", error=repr(e)[:300])
        return None, groups, plan.count
    emit("decision", "Reply read", stage="read_reply", rules=found or [], fallback=found is None)
    return found, groups, plan.count
