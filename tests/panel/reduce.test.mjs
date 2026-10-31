// The trace reducer: runs, stages, model-call chips and run status (DESIGN.md §4–6).
import { test } from "node:test";
import assert from "node:assert/strict";
import { STAGES, hostLabel, initialState, latestRun, reduce } from "../../anville/panel/static/reduce.js";
import { fold, load, runOf, upTo } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;
const FALLBACK = load("part_a_fallback").events;
const statuses = (run) => Object.fromEntries(STAGES.map((s) => [s, run.stages[s].status]));

test("the rail is the job's steps, then the agent's stages, in order", () => {
  assert.deepEqual(STAGES, ["export", "geometry", "facts", "intent", "plan", "decide", "check", "verify", "publish"]);
});

test("live run ends done with every stage done and three host-labelled chips", () => {
  const run = latestRun(fold(LIVE));
  assert.equal(run.status, "done");
  assert.equal(run.trigger, "version");
  assert.deepEqual(Object.values(statuses(run)), STAGES.map(() => "done"));
  assert.deepEqual(run.calls.map((c) => c.hostLabel), ["Baseten", "Baseten", "Meta API"]);
  assert.deepEqual(run.calls.map((c) => c.latencyS), [6.1, 7.4, 3.9]);
  assert.deepEqual(run.calls.map((c) => c.cachedTokens), [1152, 1152, 1152]);
  assert.deepEqual(run.calls.map((c) => c.completionTokens), [842, 611, 203]);
  assert.equal(run.calls[2].model, "muse-spark-1.3");
  assert.deepEqual(run.liveCalls, []);
  assert.equal(run.commentPosted, true);
});

test("mid-run state is live with the current stage active", () => {
  const run = latestRun(fold(upTo(LIVE, "Issues chosen")));  // through "Issues chosen"
  assert.equal(run.status, "live");
  assert.equal(run.current, "plan");
  assert.equal(run.stages.plan.status, "active");
  assert.equal(run.stages.intent.status, "done");
  assert.equal(run.stages.decide.status, "pending");
  assert.deepEqual(run.liveCalls, [{ stage: "plan", model: "meta-models/muse-spark-open-1026",
    host: "model-x.api.baseten.co", hostLabel: "Baseten", attempt: 1, answered: true }]);
  assert.deepEqual(run.calls, []);
});

test("fallback run: plan retried once and done, decide fell back, chips without latency", () => {
  const run = runOf(fold(FALLBACK), "v7aaaaaa-1");
  assert.equal(run.status, "done");
  assert.deepEqual([run.stages.plan.status, run.stages.plan.retries], ["done", 1]);
  assert.deepEqual([run.stages.decide.status, run.stages.decide.retries], ["fallback", 2]);
  assert.deepEqual(run.calls.map((c) => c.stage), ["plan", "plan", "decide", "decide"]);
  assert.ok(run.calls.every((c) => c.latencyS === null));
});

test("a superseded run ends superseded with no active stage", () => {
  const run = runOf(fold(FALLBACK), "v6bbbbbb-1");
  assert.equal(run.status, "superseded");
  assert.ok(Object.values(run.stages).every((s) => s.status !== "active"));
});

test("latestRun is the most recently started run", () => {
  assert.equal(latestRun(fold(FALLBACK)).id, "v7aaaaaa-1");
  assert.equal(latestRun(initialState()), null);
});

test("an event at or below the state's cursor changes nothing", () => {
  const state = fold(LIVE);
  assert.deepEqual(reduce(state, LIVE[3]), state);
});

test("reduce does not mutate its input state", () => {
  const before = fold(LIVE.slice(0, 5));
  const copy = structuredClone(before);
  reduce(before, LIVE[5]);
  assert.deepEqual(before, copy);
});

test("agent failure is flagged and the run still ends done (Review Focus 4)", () => {
  const head = LIVE.slice(0, 5);
  const t = head.at(-1).ts;
  const mk = (cursor, kind, title, detail = {}) => ({ ...head[0], cursor, seq: cursor, ts: t + cursor, kind, title, detail });
  const run = latestRun(fold([...head,
    mk(100, "error", "Agent failed; posting the setup count instead", { error: "KeyError('x')" }),
    mk(101, "comment_posted", "Posted the review"),
    mk(102, "run_finished", "Review finished")]));
  assert.equal(run.agentFailed, true);
  assert.equal(run.status, "done");
  assert.ok(Object.values(run.stages).every((s) => s.status !== "active"));
});

test("a failed review ends in error", () => {
  const head = LIVE.slice(0, 3);
  const mk = (cursor, kind, title) => ({ ...head[0], cursor, seq: cursor, ts: head[2].ts + 1, kind, title, detail: {} });
  const run = latestRun(fold([...head, mk(50, "error", "Review failed"), mk(51, "run_finished", "Review ended with an error")]));
  assert.equal(run.status, "error");
});

test("no verifier marks the verify stage skipped", () => {
  const head = LIVE.slice(0, 5);
  const mk = (cursor, title, stage) => ({ ...head[0], cursor, seq: cursor, ts: head[4].ts + 1, kind: "error", title, detail: { stage } });
  const run = latestRun(fold([...head, mk(60, "No verifier: model prose is not published", "verify")]));
  assert.equal(run.stages.verify.status, "skipped");
});

test("\"Used what the shop learned\" lights learned and names the tools (main, PR #26)", () => {
  const events = structuredClone(LIVE);
  events.splice(2, 0, { ...events[2], cursor: 2.5, kind: "tool_result", title: "Used what the shop learned",
    detail: { tools: { "910480": { review: "v6", decision: "shop_note:abc", option: "add_tool/G5/+Z/BC45-910480", on: "2026-10-30" } } } });
  const run = latestRun(fold(events));
  assert.equal(run.ledgerUsed, true);
  assert.deepEqual(run.learned, ["910480"]);
  assert.equal(latestRun(fold(LIVE)).ledgerUsed, false);
});

test("hostLabel names the sponsor, or shows the raw host, or a dash", () => {
  assert.equal(hostLabel("model-x.api.baseten.co"), "Baseten");
  assert.equal(hostLabel("api.meta.ai"), "Meta API");
  assert.equal(hostLabel("api.fireworks.ai"), "Fireworks");
  assert.equal(hostLabel("example.org"), "example.org");
  assert.equal(hostLabel(null), "—");
});

// PR #24: "Model called" (kind tool_call; stage, model, host, attempt) is emitted before each model call.
test("a model call shows on its chip, with model and host, before it answers", () => {
  const run = latestRun(fold(upTo(LIVE, "Model called")));
  assert.equal(run.current, "plan");
  assert.equal(run.stages.plan.status, "active");
  assert.deepEqual(run.liveCalls, [{ stage: "plan", model: "meta-models/muse-spark-open-1026",
    host: "model-x.api.baseten.co", hostLabel: "Baseten", attempt: 1, answered: false }]);
  assert.equal(latestRun(fold(upTo(LIVE, "Model answered"))).liveCalls[0].answered, true);
});

test("a trace from before PR #24 still gets a chip per answer", () => {
  const old = LIVE.filter((e) => e.title !== "Model called");
  assert.deepEqual(latestRun(fold(upTo(old, "Model answered"))).liveCalls,
    [{ stage: "plan", model: null, host: null, hostLabel: "—", attempt: 1, answered: true }]);
});

test("a failed call keeps its latency and error, and has no token counts", () => {
  const events = structuredClone(LIVE);
  const fin = events.find((e) => e.title === "Agent finished");
  fin.detail.calls[2] = { stage: "verify", model: "muse-spark-1.3", host: "api.meta.ai", latency_s: 30.0,
    error: "model unavailable: timed out" };
  const c = latestRun(fold(events)).calls[2];
  assert.deepEqual([c.latencyS, c.error, c.promptTokens, c.completionTokens], [30, "model unavailable: timed out", null, null]);
  assert.equal(latestRun(fold(LIVE)).calls[0].error, null);
});
