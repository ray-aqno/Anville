"""S8, the shop-note half of the ring: what the machinist reports after running a setup sheet becomes ledger
entries. A catalog cutter ticked as arrived becomes `tool_acquired`; everything else stays the shop's words.
The note's shape is the panel's P2 traveler (anville-panel-1b, 2026-10-30; ADR 0007 Amendment 2 when P2 lands)."""

import json
from datetime import datetime, timezone

import pytest

from anville.geometry import HARVEY_910480, ShopProfile, SoftJaws, Tool
from anville.shop import SHOP_NOTE, TOOL_ACQUIRED, apply_ledger, shop_note_entries
from anville.shop.sheet import _why

SHOP = ShopProfile(tools=[Tool("EM12", 12.0, 4.0)], fixture=SoftJaws())
ON = datetime(2026, 10, 30, 15, tzinfo=timezone.utc).timestamp()
NOTE = {"event": "shop.note", "note_id": "6f1c2a", "sheet_id": "sheet:part-a/v3", "part": "abc123",
        "outcome": "as_planned", "tools_arrived": [], "tags": [], "setups_run": 1, "note_text": "", "received_at": ON}
SHEET = {"part": {"source_sha256": "abc123"},
         "setups": [{"tools": [{"name": "EM12", "type": "endmill", "owned": True},
                               {"name": "910480", "type": "back_chamfer", "owned": False}]}]}


def _stored(entries):
    """What Store.read_ledger hands back: JSON round-tripped, plus the row's own fields."""
    return [{"id": i, "ledger": "shop_outcome", "scope": "shop:dll", "created_at": ON, **json.loads(json.dumps(e))}
            for i, e in enumerate(entries, 1)]


def test_every_note_is_kept_as_the_shops_report():
    (lesson,) = shop_note_entries({**NOTE, "tags": ["extra_setup"]}, sheet=SHEET)
    assert lesson["payload"] == {"kind": SHOP_NOTE, "outcome": "as_planned", "sheet_id": "sheet:part-a/v3",
                                 "part": "abc123", "setups_run": 1, "tags": ["extra_setup"], "tools_arrived": [],
                                 "note_text": "", "basis": "reported by the shop"}
    assert lesson["provenance"] == {"decision": "shop_note:6f1c2a", "sheet": "sheet:part-a/v3"}


def test_the_part_comes_from_the_stored_sheet_not_the_note():
    (lesson,) = shop_note_entries({**NOTE, "part": "forged"}, sheet=SHEET)
    assert lesson["payload"]["part"] == "abc123"


def test_a_catalog_cutter_ticked_as_arrived_becomes_an_acquisition():
    entries = shop_note_entries({**NOTE, "tools_arrived": ["910480"], "tags": ["tool_arrived"]}, sheet=SHEET)
    assert [e["payload"]["kind"] for e in entries] == [SHOP_NOTE, TOOL_ACQUIRED]
    tool = entries[1]["payload"]["tool"]
    assert tool["name"] == "910480" and tool["head_diameter"] == HARVEY_910480.head_diameter
    assert entries[1]["provenance"] == {"decision": "shop_note:6f1c2a", "sheet": "sheet:part-a/v3"}


def test_the_shop_then_owns_the_cutter_and_the_sheet_says_it_came_from_the_note():
    shop, provenance = apply_ledger(SHOP, _stored(shop_note_entries({**NOTE, "tools_arrived": ["910480"]}, sheet=SHEET)))
    assert [t.name for t in shop.back_chamfer_tools] == ["910480"]
    assert _why(provenance["910480"]) == "bought on 2026-10-30, from shop note 6f1c2a"


def test_a_cutter_that_arrived_counts_even_when_the_run_went_differently():
    """The tick is the shop's word that it owns the cutter; the run going differently doesn't undo that."""
    note = {**NOTE, "outcome": "different", "tools_arrived": ["910480"], "note_text": "Chattered; flipped it."}
    assert [e["payload"]["kind"] for e in shop_note_entries(note, sheet=SHEET)] == [SHOP_NOTE, TOOL_ACQUIRED]


def test_a_ticked_tool_that_isnt_a_back_chamfer_row_stays_the_shops_words():
    """Codex, shop notes: matching by name alone made an endmill row that shares a catalog name an acquisition."""
    sheet = {**SHEET, "setups": [{"tools": [{"name": "910480", "type": "endmill", "owned": False}]}]}
    (lesson,) = shop_note_entries({**NOTE, "tools_arrived": ["910480"]}, sheet=sheet)
    assert lesson["payload"]["tools_arrived"] == ["910480"]
    assert apply_ledger(SHOP, _stored([lesson])) == (SHOP, {})


def test_a_different_run_keeps_the_text_verbatim_and_never_reads_a_cutter_out_of_it():
    text = "Had to flip it after all: the 910480 chattered, so we cut the chamfer from the bottom."
    (lesson,) = shop_note_entries({**NOTE, "outcome": "different", "setups_run": 2, "note_text": text}, sheet=SHEET)
    assert lesson["payload"]["outcome"] == "different" and lesson["payload"]["note_text"] == text
    assert lesson["payload"]["setups_run"] == 2


def test_missing_optional_fields_are_recorded_empty():
    note = {k: NOTE[k] for k in ("note_id", "sheet_id", "outcome")}
    (lesson,) = shop_note_entries(note, sheet=SHEET)
    assert lesson["payload"]["setups_run"] is None and lesson["payload"]["tools_arrived"] == []
    assert lesson["payload"]["tags"] == [] and lesson["payload"]["note_text"] == ""


@pytest.mark.parametrize("bad, match", [
    ({"note_id": ""}, "note_id"),
    ({"sheet_id": ""}, "sheet_id"),
    ({"outcome": "fine"}, "outcome"),
    ({"outcome": "different", "note_text": "  "}, "what was different"),
    ({"setups_run": 0}, "setups_run"),
    ({"tools_arrived": ["910481"]}, "not on this sheet"),
    ({"tools_arrived": ["EM12"]}, "already owns"),  # Codex: a forged tick on an owned row
    ({"tools_arrived": {"910480": False}}, "tools_arrived"),  # Codex: a dict's keys read as ticks
    ({"tools_arrived": "910480"}, "tools_arrived"),
    ({"tags": "tool_arrived"}, "tags"),
    ({"setups_run": True}, "setups_run"),
    ({"setups_run": "1"}, "setups_run"),
    ({"note_text": 7}, "note_text"),
    ({"tools_arrived": {}}, "tools_arrived"),  # Codex, second pass: falsy wrong types were emptied, not refused
    ({"tags": False}, "tags"),
    ({"note_text": 0}, "note_text"),
    ({"note_id": True}, "note_id"),
    ({"sheet_id": ["s1"]}, "sheet_id"),
])
def test_a_note_that_cant_be_traced_or_read_is_refused(bad, match):
    with pytest.raises(ValueError, match=match):
        shop_note_entries({**NOTE, **bad}, sheet=SHEET)
