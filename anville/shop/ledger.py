"""S8 shop ledger seed: what the shop owns because of a decision, and where that came from.

Pure functions over the `shop_outcome` ledger's entries (ADR 0006 §2): `tool_acquired` builds the
entry when a catalog cutter is bought; `apply_ledger` rebuilds the shop as its ledger says it is now.
The main session stores and reads the entries (`Store.append_ledger`, `Store.read_ledger`).

After Part A's review decides to buy the 910480, the shop owns it, so Part B is planned with it
from the start, and the plan can say which review and which decision put it there.

`shop_note_entries` is the other way in: what the machinist reports after running a setup sheet. ADR 0006 §2
writes ledgers only after the planner verifies a result; a shop note is the shop's report, not the planner's,
so it is kept as the shop's words (`basis`). Only a catalog cutter ticked from the sheet's own tool rows
becomes an acquisition.
"""

from dataclasses import fields, replace
from datetime import datetime, timezone

from anville.geometry.edits import Option
from anville.geometry.shop import BACK_CHAMFER_CATALOG, BackChamferTool, ShopProfile

TOOL_ACQUIRED = "tool_acquired"
SHOP_NOTE = "shop_note"
OUTCOMES = ("as_planned", "different")
_CUTTER_FIELDS = tuple(f.name for f in fields(BackChamferTool) if f.name != "head_flat")


def tool_acquired(option: Option, *, review: str, decision: str) -> dict:
    """The ledger entry for buying the catalog back-chamfer cutter an `add_tool` option offered.

    `review` names the review that offered it (its run or event), `decision` what approved it (the
    designer's reply or the shop's confirmation). Returns {"payload", "provenance"} for append_ledger.
    """
    p = option.params
    if option.kind != "add_tool" or p.get("type") != "back_chamfer" or p.get("owned") is not False:
        raise ValueError(f"{option.key} doesn't buy a catalog back-chamfer cutter")
    if not review or not decision:
        raise ValueError("an acquisition needs provenance: the review that offered it and the decision that approved it")
    return _acquisition({k: p[k] for k in _CUTTER_FIELDS}, {"review": review, "decision": decision, "option": option.key})


def shop_note_entries(note: dict, *, sheet: dict) -> list[dict]:
    """The ledger entries for one shop note from the panel's traveler, as {"payload", "provenance"} for append_ledger.

    `sheet` is the stored setup sheet the note answers (`setup_sheet`'s dict), loaded by the caller from
    `note["sheet_id"]`: the sheet carries no id of its own, so that match is the caller's. The part comes from
    the sheet, and a tool ticked as arrived must be one of its rows the shop didn't own. Every note is kept as
    the shop's report, its text verbatim. A ticked back-chamfer row naming a catalog cutter also becomes
    `tool_acquired`, decided by "shop_note:<note_id>", whatever the outcome: the run going differently doesn't
    undo the arrival. Nothing is read out of the free text.
    """
    for key in ("note_id", "sheet_id"):
        if not isinstance(note.get(key), str) or not note[key]:
            raise ValueError(f"a shop note needs its {key}, as text: the ledger traces every entry to it")
    if note.get("outcome") not in OUTCOMES:
        raise ValueError(f"a shop note's outcome is one of {OUTCOMES}, not {note.get('outcome')!r}")
    text, runs = _given(note, "note_text", ""), note.get("setups_run")
    arrived, tags = _given(note, "tools_arrived", []), _given(note, "tags", [])
    if not isinstance(text, str):
        raise ValueError("note_text must be text")
    if note["outcome"] == "different" and not text.strip():
        raise ValueError("a 'different' note needs the machinist's words: what was different")
    if runs is not None and (type(runs) is not int or runs < 1):
        raise ValueError(f"setups_run must be a whole number of at least 1, not {runs!r}")
    for key, names in (("tools_arrived", arrived), ("tags", tags)):
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"{key} must be a list of names, not {names!r}")
    rows = {t["name"]: t for s in sheet["setups"] for t in s["tools"]}
    for name in arrived:
        if name not in rows:
            raise ValueError(f"{name!r} was ticked as arrived but is not on this sheet")
        if rows[name].get("owned", True):
            raise ValueError(f"the sheet says the shop already owns {name!r}: it can't arrive")
    provenance = {"decision": f"shop_note:{note['note_id']}", "sheet": note["sheet_id"]}
    payload = {"kind": SHOP_NOTE, "outcome": note["outcome"], "sheet_id": note["sheet_id"],
               "part": sheet["part"]["source_sha256"], "setups_run": runs, "tags": list(tags),
               "tools_arrived": list(arrived), "note_text": text, "basis": "reported by the shop"}
    entries = [{"payload": payload, "provenance": provenance}]
    catalog = {c.name: c for c in BACK_CHAMFER_CATALOG}
    for name in arrived:
        if rows[name]["type"] == "back_chamfer" and name in catalog:
            entries.append(_acquisition({k: getattr(catalog[name], k) for k in _CUTTER_FIELDS}, dict(provenance)))
    return entries


def _given(note: dict, key: str, default):
    """A missing or null field takes its default; any other value, even an empty or false one, is checked as sent."""
    return default if note.get(key) is None else note[key]


def _acquisition(tool: dict, provenance: dict) -> dict:
    return {"payload": {"kind": TOOL_ACQUIRED, "type": "back_chamfer", "tool": tool}, "provenance": provenance}


def apply_ledger(shop: ShopProfile, entries: list[dict]) -> tuple[ShopProfile, dict[str, dict]]:
    """The shop with every acquired cutter it doesn't already have, and each one's provenance, by name.

    Entries are oldest first, as `read_ledger` returns them. Other shop outcomes are left alone; a
    cutter bought twice keeps its first source; a cutter the shop already had isn't credited to the ledger.
    Each provenance gains "on", the UTC date the entry was written, when the row has its `created_at`.
    """
    owned = {t.name for t in shop.back_chamfer_tools}
    added, sources = [], {}
    for e in entries:
        payload = e["payload"]
        if payload.get("kind") != TOOL_ACQUIRED or payload.get("type") != "back_chamfer":
            continue
        tool = BackChamferTool(**{k: payload["tool"][k] for k in _CUTTER_FIELDS})
        if tool.name in owned:
            continue
        owned.add(tool.name)
        added.append(tool)
        sources[tool.name] = dict(e["provenance"])
        if e.get("created_at") is not None:
            sources[tool.name]["on"] = datetime.fromtimestamp(e["created_at"], timezone.utc).date().isoformat()
    if not added:
        return shop, sources
    return replace(shop, back_chamfer_tools=[*shop.back_chamfer_tools, *added]), sources
