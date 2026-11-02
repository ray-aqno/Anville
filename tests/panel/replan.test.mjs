// A re-plan run (<vid8>-r<n>, after the designer replies) makes no sheet and no picture of its own, by design: it
// shows its version's review's. Rayyan, live on g3: "after the initial call the 'Open setup sheet' gets grayed out."
import { test } from "node:test";
import assert from "node:assert/strict";
import { latestRun, withVersion } from "../../anville/panel/static/reduce.js";
import { renderHTML } from "../../anville/panel/static/view.js";
import { fold, load } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;
const SIG = "b".repeat(64);
const URL_V1 = `https://ray-aqno--anville-web.modal.run/sheet/${"1".repeat(32)}?s=${SIG}`;
const RENDER = (vid) => `/panel/render/${vid}?documentId=dSYN&t=x`;

// The review run, with its sheet and SAM's picture, as main emits them (sheet before posting; anchor after).
function review(vid = LIVE[0].version_id, runId = LIVE[0].run_id, from = 0) {
  const at = LIVE.findIndex((e) => e.title === "Posted the review");
  const base = LIVE.map((e) => ({ ...e, version_id: vid, run_id: runId }));
  const extra = (n, kind, title, detail) => ({ ...base.at(-1), kind, title, detail, face_ids: [] });
  const evs = [...base.slice(0, at), extra(0, "tool_result", "Setup sheet ready",
    { sheet_id: "1".repeat(32), setups: 1, buying: [], sheet_url: URL_V1 }), ...base.slice(at),
  extra(0, "tool_call", "Model called", { stage: "anchor", model: "sam-3.1", host: "api.meta.ai" }),
  extra(0, "tool_result", "Face anchored", { group: "G11", model: "sam-3.1", host: "api.meta.ai", latency_s: 3.1,
    iou: 0.857, agreed: true, error: null, version_id: vid })];
  return evs.map((e, i) => ({ ...e, cursor: from + i + 1, seq: i + 1 }));
}

// A later run of the same events under another run id: a re-plan (same version) or a new version's review.
function rerun(prev, runId, vid = prev[0].version_id) {
  const top = prev.at(-1).cursor;
  return LIVE.map((e, i) => ({ ...e, run_id: runId, version_id: vid, cursor: top + i + 1, seq: i + 1 }));
}

test("after the designer replies, the re-plan run's card still opens its version's sheet", () => {
  const first = review();
  const state = fold([...first, ...rerun(first, `${first[0].version_id.slice(0, 8)}-r1`)]);
  assert.equal(latestRun(state).id, `${first[0].version_id.slice(0, 8)}-r1`);
  assert.equal(latestRun(state).sheet, null);  // the re-plan makes no sheet of its own
  assert.equal(withVersion(state, latestRun(state), "sheet").sheet.url, URL_V1);
  const html = renderHTML(state, null, { renderUrl: RENDER });
  assert.match(html, /<a class="sheet" href="https:\/\/ray-aqno--anville-web\.modal\.run\/sheet\//);
  assert.doesNotMatch(html, /<button class="sheet" disabled>/);
});

test("a sheet borrowed from the review says so, since a re-plan may have changed the fix (Codex P1)", () => {
  const first = review();
  const html = renderHTML(fold([...first, ...rerun(first, "v7aaaaaa-r1")]));
  assert.match(html, /<a class="sheet" [^>]*>Open setup sheet \(from the first review\)<\/a>/);
  assert.match(renderHTML(fold(first)), /<a class="sheet" [^>]*>Open setup sheet<\/a>/);  // its own: no note
});

test("the re-plan run keeps its version's picture of the face, and what SAM said", () => {
  const first = review();
  const html = renderHTML(fold([...first, ...rerun(first, "v7aaaaaa-r1")]), null, { renderUrl: RENDER });
  assert.match(html, /data-section="face"><img src="\/panel\/render\/v7aaaaaa0001\?/);
  assert.match(html.replace(/<[^>]*>/g, " "), /G11, traced by SAM 3\.1: it matches the geometry \(IoU 0\.857\)/);
});

test("a new version's review never borrows the last version's sheet or picture", () => {
  const first = review();
  const state = fold([...first, ...rerun(first, "v8bbbbbb-1", "v8bbbbbb0002")]);  // still mid-review: no sheet yet
  const html = renderHTML(state, null, { renderUrl: RENDER });
  assert.match(html, /<button class="sheet" disabled>/);
  assert.doesNotMatch(html, /<a class="sheet"/);
  assert.doesNotMatch(html, /data-section="face"/);
});

test("a run with its own sheet uses its own", () => {
  const state = fold(review());
  assert.equal(withVersion(state, latestRun(state), "sheet"), latestRun(state));
});
