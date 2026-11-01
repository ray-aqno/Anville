// The traveler's note (DESIGN.md §4.8): what the page sends for each button, before any network.
import { test } from "node:test";
import assert from "node:assert/strict";
import { notePayload, requestFor, watchPicture } from "../../anville/panel/static/sheet.js";

const base = { sheetId: "sheet-part-a-buy", requestId: "11111111-1111-4111-8111-111111111111" };

test("ran it as planned sends only the ticked tools, tagged tool_arrived", () => {
  assert.deepEqual(notePayload({ ...base, outcome: "as_planned", tools: ["910480"], tags: ["extra_setup"], setupsRun: "3", text: "x" }), {
    request_id: base.requestId, sheet_id: base.sheetId, outcome: "as_planned",
    tools_arrived: ["910480"], tags: ["tool_arrived"], setups_run: null, note_text: "" });
  assert.deepEqual(notePayload({ ...base, outcome: "as_planned", tools: [], tags: [], setupsRun: "", text: "" }).tags, []);
});

test("something was different sends its tags, setups run and text as typed", () => {
  assert.deepEqual(notePayload({ ...base, outcome: "different", tools: [], tags: ["workholding_changed"], setupsRun: "2",
    text: "it chattered, we flipped" }), {
    request_id: base.requestId, sheet_id: base.sheetId, outcome: "different", tools_arrived: [],
    tags: ["workholding_changed"], setups_run: 2, note_text: "it chattered, we flipped" });
});

test("a tick adds tool_arrived once, and an empty or bad setups count is null", () => {
  const p = notePayload({ ...base, outcome: "different", tools: ["910480"], tags: ["tool_arrived"], setupsRun: "0", text: "t" });
  assert.deepEqual(p.tags, ["tool_arrived"]);
  assert.equal(p.setups_run, null);
  assert.equal(notePayload({ ...base, outcome: "different", tools: [], tags: [], setupsRun: "2.5", text: "t" }).setups_run, null);
});

test("a cutter ticked in two setups is sent once (Codex P2: two tool_acquired entries)", () => {
  assert.deepEqual(notePayload({ ...base, outcome: "as_planned", tools: ["910480", "910480"], tags: [], setupsRun: "", text: "" })
    .tools_arrived, ["910480"]);
});

test("a retry reuses the request id only while the note is unchanged (Codex P2: an edited retry was dropped)", () => {
  const note = { outcome: "different", tools: [], tags: [], setupsRun: "", text: "flipped" };
  const ids = ["id-1", "id-2"];
  const first = requestFor(null, note, () => ids.shift());
  assert.equal(first.id, "id-1");
  assert.equal(requestFor(first, { ...note }, () => ids.shift()).id, "id-1");             // same note: same id
  assert.equal(requestFor(first, { ...note, text: "flipped twice" }, () => ids.shift()).id, "id-2");  // edited: new id
});

// A sheet opened before SAM finishes (Codex P2, PR #51): the hidden slot asks again every few seconds, for a while.
function fakeSlot(results) {
  const img = { src: "", onload: null, onerror: null };
  const figure = { hidden: true, dataset: { render: "/sheet/s1/render?s=abc" }, querySelector: () => img };
  const waits = [];
  const wait = (fn, ms) => { waits.push(ms); fn(); };
  let n = 0;
  Object.defineProperty(img, "src", { set(v) { this._src = v; this.srcs = [...(this.srcs ?? []), v];
    (results[n++] ?? false) ? this.onload() : this.onerror(); }, get() { return this._src; } });
  return { img, figure, waits, wait };
}

test("the sheet's picture slot retries until the picture lands, then shows it", () => {
  const { img, figure, waits, wait } = fakeSlot([false, false, true]);
  watchPicture(figure, { every: 5000, tries: 12, wait });
  assert.equal(figure.hidden, false);
  assert.deepEqual(img.srcs, ["/sheet/s1/render?s=abc&try=1", "/sheet/s1/render?s=abc&try=2", "/sheet/s1/render?s=abc&try=3"]);
  assert.deepEqual(waits, [5000, 5000]);
});

test("the sheet's picture slot gives up quietly after its tries", () => {
  const { img, figure, waits, wait } = fakeSlot([]);
  watchPicture(figure, { every: 5000, tries: 12, wait });
  assert.equal(figure.hidden, true);
  assert.equal(img.srcs.length, 12);
  assert.equal(waits.length, 11);
});
