// Decision cards, sentence verdicts and the ring, derived from the trace (DESIGN.md §4.3–4.8).
import { test } from "node:test";
import assert from "node:assert/strict";
import { costSummary, decisionCards, latestRun, optionLabel, optionText, ringState, sentenceSummary } from "../../anville/panel/static/reduce.js";
import { fold, load, runOf, upTo } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;
const FALLBACK = load("part_a_fallback").events;
const decisionAt = (events) => events.findIndex((e) => e.title === "Decision made");

test("optionLabel renders every option key form in plain words", () => {
  assert.equal(optionLabel("add_tool/G5/+Z/BC45-910480"), "Buy the 910480 back-chamfer cutter (45°)");
  assert.equal(optionLabel("add_tool/G2/-Z/EM3"), "Buy a longer-reach EM3");
  assert.equal(optionLabel("route_machine/5"), "Send it to an outside 5-axis shop");
  assert.equal(optionLabel("remove_feature/G5"), "Remove G5");
  assert.equal(optionLabel("set_min_radius/G2/r3"), "Open up G2's inside corner radius to 3 mm");
  assert.equal(optionLabel("set_depth/G4/+Z/12.5"), "Change G4's depth to 12.5 mm");
  assert.equal(optionLabel("something_new/x"), "something_new/x");
});

test("the live run has one card: weld chamfer kept on the design side, the cutter on the shop side", () => {
  assert.deepEqual(decisionCards(latestRun(fold(LIVE))), [{
    issueId: "I1", kind: "propose", optionKey: "add_tool/G5/+Z/BC45-910480", before: 2, after: 1,
    design: { text: "G5 kept", note: "stated intent: weld prep" },
    shop: { text: "Buy the 910480 back-chamfer cutter (45°)", note: "pays back in — parts" },
    question: null, cost: null,
  }]);
});

test("no card before the decision lands", () => {
  assert.deepEqual(decisionCards(latestRun(fold(LIVE.slice(0, decisionAt(LIVE))))), []);
});

// Main's PR #26: the shop lane's Route fields arrive flat on "Option scored", not nested.
const priced = (fields) => {
  const events = structuredClone(LIVE);
  Object.assign(events.find((e) => e.kind === "option" && e.detail.key.startsWith("add_tool")).detail,
    { one_time: [180, 240], costs: { 10: [900, 1300] }, complete: true, unpriced: [], ...fields });
  return events;
};

test("a question and the option's flat cost fields reach the card", () => {
  const events = priced({ payback: [9, 14], payback_mid: 11 });
  events[decisionAt(events)].detail.question = "If the chamfer isn't needed for welding, can it go?";
  const [card] = decisionCards(latestRun(fold(events)));
  assert.equal(card.question, "If the chamfer isn't needed for welding, can it go?");
  assert.deepEqual(card.cost.one_time, [180, 240]);
  assert.equal(card.shop.note, "pays back from ~11 parts (9–14)");
});

test("the payback note never fills in an end of the range that never pays off", () => {
  const note = (fields) => decisionCards(latestRun(fold(priced(fields))))[0].shop.note;
  assert.equal(note({ payback: null, payback_mid: null }), "payback not claimed");
  assert.equal(note({ payback: [null, null], payback_mid: null }), "doesn't pay off at these inputs");
  assert.equal(note({ payback: [6, null], payback_mid: null }), "pays off past 6 parts at the best inputs only");
  assert.equal(note({ payback: [6, null], payback_mid: 11 }), "pays back from ~11 parts (6 at best; never at worst)");
});

test("an ask decision is a flag card with its question and no option", () => {
  const events = structuredClone(LIVE);
  Object.assign(events[decisionAt(events)].detail,
    { action: "ask", option_key: null, question: "Is G5 required as modelled?", reason: "No shop-side fix reaches G5." });
  assert.deepEqual(decisionCards(latestRun(fold(events))), [{
    issueId: "I1", kind: "ask", question: "Is G5 required as modelled?" }]);
});

test("cost verdicts say depends when nothing is recommended, never headline the leader, and name placeholders", () => {
  const events = structuredClone(LIVE);
  const at = events.findIndex((e) => e.title === "Decision made");
  events.splice(at, 0, { ...events[at], cursor: events[at].cursor - 0.5, kind: "decision", title: "Costs compared",
    detail: { stage: "plan", scenarios: {},
      verdicts: [{ quantity: 1, recommended: "current", leader: "current", flip: null },
                 { quantity: 10, recommended: null, leader: "remove_feature/G5", flip: { corner: "hi", winner: "x", by: 3 } },
                 { quantity: 50, recommended: "add_tool/G5/+Z/BC45-910480", leader: "add_tool/G5/+Z/BC45-910480", flip: null }],
      inputs: { designer_rate: { lo: 0, hi: 0, unit: "$/h", status: "placeholder", source: "unpriced" },
                shop_rate: { lo: 85, hi: 120, unit: "$/h", status: "sourced", source: "x" },
                setup_minutes: { lo: 30, hi: 60, unit: "min", status: "placeholder", source: "y" } } } });
  assert.deepEqual(costSummary(latestRun(fold(events))), {
    verdicts: [{ quantity: 1, text: "Keep the current plan" }, { quantity: 10, text: "depends" },
               { quantity: 50, text: "Buy the 910480 back-chamfer cutter (45°)" }],
    placeholders: ["designer_rate", "setup_minutes"] });
  assert.equal(costSummary(latestRun(fold(LIVE))), null);
});

test("a part-side option on a free face fills the design side; the shop side shows no change", () => {
  const events = structuredClone(LIVE);
  events.find((e) => e.title === "Intent tagged and ask gate computed").detail.intents.G5 = { status: "unknown", reason: "" };
  const opt = events.find((e) => e.kind === "option" && e.detail.key.startsWith("route"));
  Object.assign(opt.detail, { key: "remove_feature/G5", side: "part" });
  events[decisionAt(events)].detail.option_key = "remove_feature/G5";
  const [card] = decisionCards(latestRun(fold(events)));
  assert.deepEqual(card.design, { text: "Remove G5", note: null });
  assert.deepEqual(card.shop, { text: "No change", note: null });
});

test("quiet, unknown actions and unoffered options make no card and never throw", () => {
  for (const patch of [{ action: "quiet" }, { action: "dance" }, { option_key: "add_tool/G9/x/y" }]) {
    const events = structuredClone(LIVE);
    Object.assign(events[decisionAt(events)].detail, patch);
    assert.deepEqual(decisionCards(latestRun(fold(events))), [], JSON.stringify(patch));
  }
});

test("verify indexes map to the sentences that passed the check (Review Focus 3)", () => {
  const s = sentenceSummary(latestRun(fold(LIVE)));
  assert.equal(s.verified, 2);
  assert.equal(s.total, 4);
  assert.deepEqual(s.items.map((i) => i.status), ["verified", "rejected-verify", "rejected-check", "verified"]);
  assert.equal(s.items[1].reason, "says more than its cites");
  assert.equal(s.items[3].text, "Buying the back-chamfer cutter takes this part from 2 setups to 1.");
});

test("sentences without text still map, with text null", () => {
  const events = structuredClone(LIVE);
  for (const e of events) delete e.detail.text;
  const s = sentenceSummary(latestRun(fold(events)));
  assert.deepEqual(s.items.map((i) => [i.status, i.text]),
    [["verified", null], ["rejected-verify", null], ["rejected-check", null], ["verified", null]]);
});

test("checked sentences with no verify verdict are unverified", () => {
  const verifyAt = LIVE.findIndex((e) => e.title === "Model answered" && e.detail.stage === "verify");
  const s = sentenceSummary(latestRun(fold(LIVE.slice(0, verifyAt))));
  assert.deepEqual(s.items.map((i) => i.status), ["unverified", "unverified", "rejected-check", "unverified"]);
  assert.equal(s.verified, 0);
});

test("the fallback run published no model sentences", () => {
  assert.deepEqual(sentenceSummary(runOf(fold(FALLBACK), "v7aaaaaa-1")), { verified: 0, total: 0, items: [] });
});

test("the ring after a finished review: saved and reviewed lit, setup sheet next", () => {
  assert.deepEqual(ringState(latestRun(fold(LIVE))),
    { saved: "lit", reviewing: "lit", reviewed: "lit", setupSheet: "next", learned: "off", innerArc: "off" });
});

test("the ring mid-run: the review arc is active and nothing after it is lit", () => {
  assert.deepEqual(ringState(latestRun(fold(upTo(LIVE, "Issues chosen")))),
    { saved: "lit", reviewing: "active", reviewed: "off", setupSheet: "off", learned: "off", innerArc: "off" });
});

test("the ring lights learned when a ledger entry was used, and the inner arc on a reply", () => {
  const events = structuredClone(LIVE);
  events[0].detail.trigger = "reply";
  events.splice(2, 0, { ...events[2], cursor: 2.5, kind: "tool_result", title: "Used what the shop learned",
    detail: { tools: { "910480": { on: "2026-10-30" } } } });
  const ring = ringState(latestRun(fold(events)));
  assert.equal(ring.learned, "lit");
  assert.equal(ring.innerArc, "lit");
});

test("a superseded run never claims a review", () => {
  const ring = ringState(runOf(fold(FALLBACK), "v6bbbbbb-1"));
  assert.equal(ring.reviewed, "off");
  assert.equal(ring.reviewing, "lit");
});

// Main, PR #26: "Option scored" carries the comment's own name for the fix, so the panel reads it, not the key.
test("optionText capitalizes the trace's name and adds the numbers from params", () => {
  const o = (name, editKind, params, key = "k/x") => optionText({ key, name, editKind, params });
  assert.equal(o("buying the 910480 back-chamfer cutter", "add_tool", { type: "back_chamfer", angle: 45, owned: false }),
    "Buying the 910480 back-chamfer cutter (45°)");
  assert.equal(o("using the shop's 910480 back-chamfer cutter", "add_tool", { type: "back_chamfer", angle: 45, owned: true }),
    "Using the shop's 910480 back-chamfer cutter (45°)");
  assert.equal(o("opening up G2's inside corner radius", "set_min_radius", { radius: 3 }), "Opening up G2's inside corner radius to 3 mm");
  assert.equal(o("changing G4's depth", "set_depth", { direction: "+Z", depth: 12.5 }), "Changing G4's depth to 12.5 mm");
  assert.equal(o("sending it to an outside 5-axis shop", "route_machine", {}), "Sending it to an outside 5-axis shop");
  assert.equal(optionText({ key: "remove_feature/G5", name: null, editKind: null, params: {} }), "Remove G5");  // older traces
});

test("the card and the cost verdicts use the option's trace name", () => {
  const events = structuredClone(LIVE);
  const opt = events.find((e) => e.kind === "option" && e.detail.key.startsWith("add_tool"));
  Object.assign(opt.detail, { name: "buying the 910480 back-chamfer cutter", edit_kind: "add_tool",
    params: { type: "back_chamfer", angle: 45, owned: false } });
  const at = events.findIndex((e) => e.title === "Decision made");
  events.splice(at, 0, { ...events[at], cursor: events[at].cursor - 0.5, title: "Costs compared",
    detail: { stage: "plan", scenarios: {}, inputs: {},
      verdicts: [{ quantity: 50, recommended: opt.detail.key, leader: opt.detail.key, flip: null }] } });
  const run = latestRun(fold(events));
  assert.equal(decisionCards(run)[0].shop.text, "Buying the 910480 back-chamfer cutter (45°)");
  assert.deepEqual(costSummary(run).verdicts, [{ quantity: 50, text: "Buying the 910480 back-chamfer cutter (45°)" }]);
});
