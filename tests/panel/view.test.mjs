// The panel's HTML for one state (DESIGN.md §4): pure string output, so it is testable without a DOM.
import { test } from "node:test";
import assert from "node:assert/strict";
import { esc, renderHTML } from "../../anville/panel/static/view.js";
import { fold, load, upTo } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;
const FALLBACK = load("part_a_fallback").events;
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

test("esc escapes the five HTML characters", () => {
  assert.equal(esc(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
  assert.equal(esc(null), "");
  assert.equal(esc(3), "3");
});

test("a finished run shows the decision card, the hosts and the sentence tally", () => {
  const html = renderHTML(fold(LIVE));
  const words = text(html);
  assert.match(words, /2 → 1 setups/);
  assert.match(words, /G5 kept/);
  assert.match(words, /stated intent: weld prep/);
  assert.match(words, /Buy the 910480 back-chamfer cutter \(45°\)/);
  assert.match(words, /Meta API/);
  assert.match(words, /Baseten/);
  assert.match(words, /2 of 4 sentences verified/);
  assert.match(words, /done · 29 s/);  // run_started → run_finished in the fixture: 29.15 s
  assert.match(html, /class="ring"/);
  assert.match(html, /\$ —/);  // no cost in the trace yet: a dash, never an estimate
});

test("model free text is escaped (Review Focus 1)", () => {
  const events = structuredClone(LIVE);
  const decision = events.find((e) => e.title === "Decision made");
  decision.detail.reason = "<img src=x onerror=alert(1)>";
  events.find((e) => e.title === "Sentence checked").detail.text = "<script>alert(2)</script>";
  const html = renderHTML(fold(events));
  assert.ok(!html.includes("<img src=x"), "raw img tag rendered");
  assert.ok(!html.includes("<script>"), "raw script tag rendered");
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
});

test("mid-run shows live status and no decision card yet", () => {
  const html = renderHTML(fold(upTo(LIVE, "Issues chosen")));
  assert.match(text(html), /● live · 15 s/);  // the clock runs to the latest event (15.2 s in)
  assert.ok(!html.includes('class="card"'));
  assert.match(html, /data-stage="plan" data-status="active"/);
});

test("chips without latency show a dash, and a fallback stage is marked", () => {
  const html = renderHTML(fold(FALLBACK));
  assert.match(html, /data-stage="decide" data-status="fallback"/);
  assert.match(text(html), /↺2/);
  assert.match(text(html), /— s/);
});

test("rejected edits are listed with their reason", () => {
  assert.match(text(renderHTML(fold(LIVE))), /Remove G5 .*stated intent: weld prep/);
});

test("an empty state invites the next save", () => {
  assert.match(text(renderHTML(fold([]))), /Waiting for the next save/);
});

test("earlier runs are listed with their status", () => {
  const html = renderHTML(fold(FALLBACK));
  assert.match(html, /data-run="v6bbbbbb-1"/);
  assert.match(text(html), /superseded/);
});

test("while a model call runs, its chip names the model and the host", () => {
  const words = text(renderHTML(fold(upTo(LIVE, "Model called"))));
  assert.match(words, /plan Muse Spark Open Baseten calling…/);
});

test("a failed model call says so, with its latency", () => {
  const events = structuredClone(LIVE);
  events.find((e) => e.title === "Agent finished").detail.calls[2] = { stage: "verify", model: "muse-spark-1.3",
    host: "api.meta.ai", latency_s: 30.0, error: "model unavailable: timed out" };
  assert.match(text(renderHTML(fold(events))), /verify Muse Spark 1\.3 Meta API failed after 30 s: model unavailable: timed out/);
});

test("before the verifier answers, the tally says the sentences are waiting, not failed", () => {
  const words = text(renderHTML(fold(upTo(LIVE, "Model called").concat(
    LIVE.filter((e) => e.title === "Sentence checked")))));
  assert.match(words, /3 of 4 sentences passed the check · verifying…/);
  assert.doesNotMatch(words, /0 of \d sentences verified/);
});

test("the view marks its top-level sections so the page can patch only what changed", () => {
  const html = renderHTML(fold(LIVE));
  for (const s of ["header", "ring", "cards", "agent", "earlier"]) assert.match(html, new RegExp(`data-section="${s}"`));
});

test("cost, verdicts, placeholders, learned tools and an ask flag render as words (main, PR #26)", () => {
  const events = structuredClone(LIVE);
  Object.assign(events.find((e) => e.kind === "option" && e.detail.key.startsWith("add_tool")).detail,
    { one_time: [180, 240], payback: [9, 14], payback_mid: 11, complete: true, unpriced: [] });
  const at = events.findIndex((e) => e.title === "Decision made");
  events.splice(at, 0, { ...events[at], cursor: events[at].cursor - 0.5, title: "Costs compared",
    detail: { stage: "plan", scenarios: {}, verdicts: [{ quantity: 10, recommended: null, leader: "remove_feature/G5", flip: null }],
      inputs: { designer_rate: { lo: 0, hi: 0, unit: "$/h", status: "placeholder", source: "" } } } });
  events.splice(2, 0, { ...events[2], cursor: 2.5, kind: "tool_result", title: "Used what the shop learned",
    detail: { tools: { "910480": { on: "2026-10-30" } } } });
  const words = text(renderHTML(fold(events)));
  assert.match(words, /one-time \$180–240/);
  assert.match(words, /pays back from ~11 parts \(9–14\)/);
  assert.match(words, /10 parts: depends/);
  assert.doesNotMatch(words, /Remove G5 (wins|leads)|10 parts: Remove/);
  assert.match(words, /placeholder: designer_rate/);
  assert.match(words, /Learned from the shop: 910480/);

  const ask = structuredClone(LIVE);
  Object.assign(ask[ask.findIndex((e) => e.title === "Decision made")].detail,
    { action: "ask", option_key: null, question: "Is <G5> required?", reason: "No shop fix." });
  const html = renderHTML(fold(ask));
  assert.match(text(html), /Flag Is &lt;G5&gt; required\?/);
  assert.match(text(html), /No shop fix\./);
});
