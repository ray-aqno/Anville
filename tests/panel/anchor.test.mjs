// SAM 3.1 anchoring the flagged face (ADR 0007 Amendment 4): it runs after the comment posts, so its events land
// on a finished run. The chip goes live, then gets its latency; the picture and its caption follow.
import { test } from "node:test";
import assert from "node:assert/strict";
import { latestRun } from "../../anville/panel/static/reduce.js";
import { renderHTML } from "../../anville/panel/static/view.js";
import { fold, load } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;
const last = LIVE.at(-1);
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
const ev = (n, kind, title, detail) => ({ ...last, cursor: last.cursor + n, seq: last.seq + n, ts: last.ts + n, kind,
  title, detail, face_ids: [] });
const CALLED = ev(1, "tool_call", "Model called", { stage: "anchor", model: "sam-3.1", host: "api.meta.ai" });
const anchored = (record) => ev(2, "tool_result", "Face anchored", {
  group: "G11", model: "sam-3.1", host: "api.meta.ai", latency_s: 1.17, iou: 0.99, agreed: true, error: null,
  version_id: last.version_id, ...record });
const URL = (vid) => `/panel/render/${vid}?documentId=dSYN&t=x&y`;

test("while SAM runs, its chip is live after the finished agent's chips", () => {
  const run = latestRun(fold([...LIVE, CALLED]));
  assert.equal(run.calls.length, 3);
  assert.deepEqual(run.liveCalls, [{ stage: "anchor", model: "sam-3.1", host: "api.meta.ai", hostLabel: "Meta API",
    attempt: null, answered: false }]);
  assert.equal(run.anchor, null);
  const words = text(renderHTML(fold([...LIVE, CALLED])));
  assert.match(words, /anchor SAM 3\.1 Meta API calling…/);
  assert.match(words, /verify Muse Spark 1\.3 Meta API/);  // the agent's chips stay
});

test("once the face is anchored, SAM's chip has its latency and the run has the anchor", () => {
  const run = latestRun(fold([...LIVE, CALLED, anchored()]));
  assert.deepEqual(run.liveCalls, []);
  assert.equal(run.calls.length, 4);
  assert.deepEqual(run.calls[3], { stage: "anchor", model: "sam-3.1", host: "api.meta.ai", hostLabel: "Meta API",
    promptTokens: null, completionTokens: null, cachedTokens: null, latencyS: 1.17, error: null });
  assert.deepEqual(run.anchor, { group: "G11", iou: 0.99, agreed: true, error: null, versionId: last.version_id });
  const words = text(renderHTML(fold([...LIVE, CALLED, anchored()])));
  assert.match(words, /anchor SAM 3\.1 Meta API 1\.17 s/);  // no token counts: SAM has none
  assert.doesNotMatch(words, /anchor SAM 3\.1 Meta API — in/);
});

test("a SAM call that failed shows as a failed chip", () => {
  const run = latestRun(fold([...LIVE, CALLED, anchored({ iou: null, agreed: null, error: "HTTP 503: busy" })]));
  assert.equal(run.calls[3].error, "HTTP 503: busy");
  assert.match(text(renderHTML(fold([...LIVE, CALLED, anchored({ iou: null, agreed: null, error: "HTTP 503: busy" })]))),
    /anchor SAM 3\.1 Meta API failed after 1\.17 s: HTTP 503: busy/);
});

test("no SAM configured: the picture is anchored, but no chip claims a call", () => {
  const run = latestRun(fold([...LIVE, anchored({ latency_s: null, iou: null, agreed: null,
    error: "SAM not configured (no MODEL_API_KEY)" })]));
  assert.equal(run.calls.length, 3);
  assert.equal(run.anchor.group, "G11");
});

test("the picture of the face shows under the cards, with what SAM said", () => {
  const html = renderHTML(fold([...LIVE, CALLED, anchored()]), null, { renderUrl: URL });
  assert.match(html, /<figure class="face" data-section="face"><img src="\/panel\/render\/v7aaaaaa0001\?documentId=dSYN&amp;t=x&amp;y"/);
  assert.ok(html.indexOf('data-section="face"') > html.indexOf('data-section="cards"'));
  assert.ok(html.indexOf('data-section="face"') < html.indexOf('data-section="agent"'));
  assert.match(text(html), /G11, traced by SAM 3\.1: it matches the geometry \(IoU 0\.99\)/);
});

test("a disagreeing or missing SAM trace is said plainly", () => {
  const off = text(renderHTML(fold([...LIVE, CALLED, anchored({ iou: 0.42, agreed: false })]), null, { renderUrl: URL }));
  assert.match(off, /G11, from the geometry\. SAM 3\.1 traced a different shape \(IoU 0\.42\), so its outline isn't drawn\./);
  const failed = text(renderHTML(fold([...LIVE, CALLED, anchored({ iou: null, agreed: null, error: "timeout" })]), null,
    { renderUrl: URL }));
  assert.match(failed, /G11, from the geometry\. SAM 3\.1 didn't answer\./);
  const none = text(renderHTML(fold([...LIVE, anchored({ latency_s: null, iou: null, agreed: null, error: "x" })]), null,
    { renderUrl: URL }));
  assert.match(none, /G11, from the geometry\./);
});

test("no picture before the face is anchored, or without a render URL", () => {
  assert.doesNotMatch(renderHTML(fold([...LIVE, CALLED]), null, { renderUrl: URL }), /data-section="face"/);
  assert.doesNotMatch(renderHTML(fold([...LIVE, CALLED, anchored()])), /data-section="face"/);
});
