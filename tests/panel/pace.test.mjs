// Burst replay at the recorded timing (DESIGN.md §5.4): order and relative timing are real, the wait is shortened.
import { test } from "node:test";
import assert from "node:assert/strict";
import { BURST_MS, MAX_MS, MIN_MS, createQueue, paceDelay } from "../../anville/panel/static/pace.js";
import { load } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;

test("the pacing constants are the design's", () => {
  assert.deepEqual([MIN_MS, MAX_MS, BURST_MS], [120, 1200, 250]);
});

test("paceDelay: no previous event, or events that arrived apart, render at once", () => {
  assert.equal(paceDelay(null, { ts: 10, arrivedMs: 0 }), 0);
  assert.equal(paceDelay({ ts: 10, arrivedMs: 0 }, { ts: 15, arrivedMs: 300 }), 0);
});

test("paceDelay: within a burst, the recorded gap is clamped to 120–1200 ms", () => {
  const prev = { ts: 10, arrivedMs: 1000 };
  assert.equal(paceDelay(prev, { ts: 15, arrivedMs: 1000 }), 1200);
  assert.equal(paceDelay(prev, { ts: 10.05, arrivedMs: 1010 }), 120);
  assert.equal(paceDelay(prev, { ts: 10.4, arrivedMs: 1100 }), 400);
  assert.equal(paceDelay(prev, { ts: 9, arrivedMs: 1000 }), 120);  // clock skew never stalls or reorders
});

function fakeTimers() {
  let now = 0;
  const pending = [];
  return {
    now: () => now,
    setTimer: (fn, ms) => pending.push({ at: now + ms, fn }),
    runAll() {
      while (pending.length) {
        pending.sort((a, b) => a.at - b.at);
        const t = pending.shift();
        now = t.at;
        t.fn();
      }
      return now;
    },
  };
}

test("replay applies every event in cursor order, waiting the clamped recorded gaps", () => {
  const timers = fakeTimers();
  const applied = [];
  const q = createQueue((e) => applied.push(e.cursor), timers);
  q.replay(LIVE);
  const elapsed = timers.runAll();
  assert.deepEqual(applied, LIVE.map((e) => e.cursor));
  const expected = LIVE.slice(1).reduce((sum, e, i) =>
    sum + Math.round(Math.min(MAX_MS, Math.max(MIN_MS, (e.ts - LIVE[i].ts) * 1000))), 0);
  assert.equal(elapsed, expected);
});

test("push renders an event at once when it arrives on its own", () => {
  const timers = fakeTimers();
  const applied = [];
  const q = createQueue((e) => applied.push(e.cursor), timers);
  q.push(LIVE[0]);
  assert.deepEqual(applied, [1]);
});

test("events pushed in one burst keep their order and are spaced out", () => {
  const timers = fakeTimers();
  const applied = [];
  const q = createQueue((e) => applied.push(e.cursor), timers);
  for (const e of LIVE.slice(0, 4)) q.push(e);
  assert.deepEqual(applied, [1]);
  assert.ok(timers.runAll() > 0);
  assert.deepEqual(applied, [1, 2, 3, 4]);
});

test("stop() cancels a replay's pending events (codex P2: Back to live mid-replay)", () => {
  const timers = fakeTimers();
  const applied = [];
  const q = createQueue((e) => applied.push(e.cursor), timers);
  q.replay(LIVE);
  assert.deepEqual(applied, [1]);
  q.stop();
  timers.runAll();
  assert.deepEqual(applied, [1]);
  q.push(LIVE[1]);  // the queue still works afterwards
  assert.deepEqual(applied, [1, 2]);
});
