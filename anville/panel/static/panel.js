// Wiring: the URL's documentId and t, first paint from /api/runs, then live events from /api/stream.
// Everything shown is computed by reduce.js and view.js; this file only moves events and repaints.

import { createQueue } from "./pace.js";
import { initialState, reduce } from "./reduce.js";
import { renderHTML } from "./view.js";

const params = new URLSearchParams(location.search);
const doc = params.get("documentId");
const t = params.get("t");
const url = (path, extra = {}) => `${path}?${new URLSearchParams({ documentId: doc, t, ...extra })}`;
const app = document.getElementById("app");
const renderUrl = (vid) => url(`/panel/render/${encodeURIComponent(vid)}`);  // the flagged face's picture

let state = initialState();
let replay = null;  // {runId, state}: an earlier run being replayed; null shows the live state

function draw() {
  const view = replay ? replay.state : state;
  patch(renderHTML(view, replay?.runId ?? null, { renderUrl })
    + (replay ? `<button class="back" data-section="back" data-live>Back to live</button>` : ""));
}

// Replace only the top-level sections whose HTML changed, so animations in the others keep running.
function patch(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const next = [...tpl.content.children];
  const cur = [...app.children];
  if (next.length !== cur.length || next.some((n, i) => n.dataset.section !== cur[i].dataset.section)) {
    app.replaceChildren(...next);
    return;
  }
  next.forEach((n, i) => {
    if (n.outerHTML !== cur[i].outerHTML) cur[i].replaceWith(n);
  });
}

const live = createQueue((e) => {
  state = reduce(state, e);
  if (!replay) draw();
});

const replayer = createQueue((e) => {
  replay.state = reduce(replay.state, e);
  draw();
});

app.addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-run]");
  if (row) {
    const events = Object.values(runEvents).flat().filter((e) => e.run_id === row.dataset.run);
    replay = { runId: row.dataset.run, state: initialState() };
    replayer.replay(events);
  } else if (ev.target.closest("[data-live]")) {
    replayer.stop();  // a paced replay may still have a timer pending
    replay = null;
    draw();
  }
});

const runEvents = {};  // run_id -> events seen, for replays

async function start() {
  const res = await fetch(url("/api/runs"));
  if (!res.ok) {
    app.innerHTML = `<p class="empty">This panel link isn't valid for this document.</p>`;
    return;
  }
  const { runs, cursor } = await res.json();
  const last = runs.at(-1);
  for (const r of runs) {
    runEvents[r.run_id] = r.events;
    // A finished run paints at once; a run still in progress replays at its recorded pace.
    if (r !== last || r.events.some((e) => e.kind === "run_finished")) {
      for (const e of r.events) state = reduce(state, e);
    }
  }
  draw();
  if (last && !last.events.some((e) => e.kind === "run_finished")) live.replay(last.events);

  const source = new EventSource(url("/api/stream", { after: cursor }));
  source.addEventListener("trace", (msg) => {
    const e = JSON.parse(msg.data);
    (runEvents[e.run_id] ??= []).push(e);
    live.push(e);
  });
}

start();
