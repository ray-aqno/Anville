"""S8 shop ledger seed: what the shop owns because of a decision, and where that came from.

Pure functions over the `shop_outcome` ledger's entries (ADR 0006 §2): `tool_acquired` builds the
entry when a catalog cutter is bought; `apply_ledger` rebuilds the shop as its ledger says it is now.
The main session stores and reads the entries (`Store.append_ledger`, `Store.read_ledger`).

After Part A's review decides to buy the 910480, the shop owns it, so Part B is planned with it
from the start, and the plan can say which review and which decision put it there.
"""

from dataclasses import fields, replace

from anville.geometry.edits import Option
from anville.geometry.shop import BackChamferTool, ShopProfile

TOOL_ACQUIRED = "tool_acquired"
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
    return {"payload": {"kind": TOOL_ACQUIRED, "type": "back_chamfer", "tool": {k: p[k] for k in _CUTTER_FIELDS}},
            "provenance": {"review": review, "decision": decision, "option": option.key}}


def apply_ledger(shop: ShopProfile, entries: list[dict]) -> tuple[ShopProfile, dict[str, dict]]:
    """The shop with every acquired cutter it doesn't already have, and each one's provenance, by name.

    Entries are oldest first, as `read_ledger` returns them. Other shop outcomes are left alone; a
    cutter bought twice keeps its first source; a cutter the shop already had isn't credited to the ledger.
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
        sources[tool.name] = e["provenance"]
    if not added:
        return shop, sources
    return replace(shop, back_chamfer_tools=[*shop.back_chamfer_tools, *added]), sources
