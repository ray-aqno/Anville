// Burst replay at the recorded timing (DESIGN.md §5.4). When trace events arrive together (a buffered
// flush, or a replay), they are shown in order with their real relative gaps, each clamped to 120–1200 ms.
// Events that arrive on their own render at once.

export const MIN_MS = 120, MAX_MS = 1200, BURST_MS = 250;

// prev/cur: {ts (event seconds), arrivedMs (when the panel received it)}
export function paceDelay(prev, cur) {
  if (!prev || cur.arrivedMs - prev.arrivedMs >= BURST_MS) return 0;
  return Math.round(Math.min(MAX_MS, Math.max(MIN_MS, (cur.ts - prev.ts) * 1000)));
}

export function createQueue(apply, { setTimer = setTimeout, now = Date.now } = {}) {
  let queue = [], prev = null, busy = false, generation = 0;

  function step() {
    if (!queue.length) { busy = false; return; }
    busy = true;
    const cur = queue[0], gen = generation;
    const go = () => {
      if (gen !== generation) return;  // a replay started since this was scheduled
      queue.shift();
      prev = cur;
      apply(cur.event);
      step();
    };
    const wait = paceDelay(prev, cur);
    if (wait > 0) setTimer(go, wait); else go();
  }

  return {
    push(event) {
      queue.push({ event, ts: event.ts, arrivedMs: now() });
      if (!busy) step();
    },
    // Drop anything still waiting, including timers already scheduled (e.g. leaving a replay).
    stop() {
      generation += 1;
      queue = [];
      prev = null;
      busy = false;
    },
    // Show these events from the start, as one burst: drops anything still waiting.
    replay(events) {
      generation += 1;
      const t = now();
      queue = events.map((event) => ({ event, ts: event.ts, arrivedMs: t }));
      prev = null;
      step();
    },
  };
}
