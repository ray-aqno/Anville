// State -> HTML for the docked panel (DESIGN.md §4): the ring, the decision card, then the agent's work.
// Pure string output; every value from the trace goes through esc(), because reasons and sentences are model text.

import { STAGES, costSummary, decisionCards, latestRun, optionLabel, optionText, ringState, sentenceSummary } from "./reduce.js";

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export function esc(s) {
  return s === null || s === undefined ? "" : String(s).replace(/[&<>"']/g, (c) => ESC[c]);
}

const dash = (v, unit = "") => (v === null || v === undefined ? "—" : `${v}${unit}`);

function modelName(model) {
  if (!model) return "—";
  if (/muse-spark-open/i.test(model)) return "Muse Spark Open";
  if (/muse-spark-1\.3/i.test(model)) return "Muse Spark 1.3";
  return model.split("/").at(-1);
}

function statusText(run) {
  const secs = Math.round((run.endedTs ?? run.lastTs) - run.startedTs);
  return { live: `● live · ${secs} s`, done: `done · ${secs} s`, superseded: "superseded",
    error: "ended with an error" }[run.status];
}

function ring(r) {
  const node = (x, y, cls, label, ly) =>
    `<circle class="n ${cls}" cx="${x}" cy="${y}" r="12"/><text class="nl ${cls}" x="${x}" y="${ly}">${label}</text>`;
  return `<svg class="ring" viewBox="0 0 420 206" role="img" aria-label="The loop: design, Anville, shop">
  <line class="wall" x1="210" y1="4" x2="210" y2="202"/><text class="walltag" x="216" y="108">the wall</text>
  <text class="side design" x="10" y="16">DESIGN</text><text class="side shop" x="410" y="16">SHOP</text>
  <ellipse class="track" cx="210" cy="100" rx="160" ry="72"/>
  <path class="arc ${r.reviewing}" d="M 50 100 A 160 72 0 0 1 210 28"/>
  <path class="arc ${r.setupSheet}" d="M 210 28 A 160 72 0 0 1 370 100"/>
  <path class="arc ${r.learned}" d="M 370 100 A 160 72 0 0 1 210 172"/>
  <path class="arc ${r.learned}" d="M 210 172 A 160 72 0 0 1 50 100"/>
  <path class="arc inner ${r.innerArc}" d="M 66 92 Q 104 46 194 38"/><text class="arctag" x="84" y="86">reply ⇄ re-plan</text>
  ${node(50, 100, `design ${r.saved}`, "Saved", 130)}
  ${node(210, 28, `anville ${r.reviewed}`, "Reviewed", 58)}
  ${node(370, 100, `shop ${r.setupSheet}`, "Setup sheet", 130)}
  ${node(210, 172, `anville ${r.learned}`, "Shop note → learned", 200)}
</svg>`;
}

function card(c, reason) {
  const cost = c.cost?.one_time ? `one-time $${esc(c.cost.one_time[0])}–${esc(c.cost.one_time[1])}` : "$ —";
  const half = (cls, title, h) => `<div class="half ${cls}"><div class="tag">${title}</div>
    <div class="what">${esc(h.text)}</div>${h.note ? `<div class="note">${esc(h.note)}</div>` : ""}</div>`;
  return `<section class="card" data-option="${esc(c.optionKey)}">
  <div class="headline"><b>${esc(dash(c.before))}</b> → <b class="after">${esc(dash(c.after))}</b> <span>setups</span></div>
  <div class="wall2">${half("design", "Design side", c.design)}${half("shop", "Shop side", c.shop)}</div>
  ${c.question ? `<div class="question">${esc(c.question)}</div>` : ""}
  ${reason ? `<div class="why"><span class="tag">Why</span> ${esc(reason)}</div>` : ""}
  <div class="cost">${cost}</div>
  <button class="sheet" disabled>Open setup sheet</button>
</section>`;
}

function askCard(c, reason) {
  return `<section class="card ask" data-issue="${esc(c.issueId)}"><div class="tag">Flag</div>
  <div class="question">${esc(c.question)}</div>
  ${reason ? `<div class="why"><span class="tag">Why</span> ${esc(reason)}</div>` : ""}
</section>`;
}

function costStrip(s) {
  if (!s || !s.verdicts.length) return "";
  return `<div class="verdicts"><div class="tag">Cheapest by quantity</div><ul>${
    s.verdicts.map((v) => `<li>${esc(v.quantity)} parts: ${esc(v.text)}</li>`).join("")}</ul>${
    s.placeholders.length ? `<div class="placeholders">placeholder: ${esc(s.placeholders.join(", "))}</div>` : ""}</div>`;
}

function rail(run) {
  return `<ol class="rail">${STAGES.map((s) => {
    const st = run.stages[s];
    const retries = st.retries ? ` <span class="retry">↺${st.retries}</span>` : "";
    return `<li data-stage="${s}" data-status="${st.status}">${s}${retries}</li>`;
  }).join("")}</ol>`;
}

function chips(run) {
  const head = (c) => {
    const host = esc(c.hostLabel);
    return `<b>${esc(c.stage)}</b> <span class="model">${esc(modelName(c.model))}</span>
  <span class="host" data-host="${host}">${host}</span>`;
  };
  if (run.calls.length) {
    return run.calls.map((c) => c.error
      ? `<li class="chip failed">${head(c)} <span class="num">failed after ${esc(dash(c.latencyS))} s: ${esc(c.error)}</span></li>`
      : `<li class="chip">${head(c)}
  <span class="num">${esc(dash(c.promptTokens))} in · ${esc(dash(c.completionTokens))} out · ${esc(dash(c.cachedTokens))} cached · ${esc(dash(c.latencyS))} s</span></li>`).join("");
  }
  // During the run: one chip per call as it starts ("Model called"); tokens and latency arrive with "Agent finished".
  return run.liveCalls.map((c) => `<li class="chip pending${c.answered ? "" : " calling"}">${head(c)}
  <span class="num">${c.answered ? "answered" : "calling…"}${c.attempt > 1 ? ` · attempt ${esc(c.attempt)}` : ""}</span></li>`).join("");
}

function options(run) {
  const scored = Object.values(run.options).map((o) =>
    `<li class="opt ${esc(o.side)}">${esc(optionText(o))} <span class="num">${esc(dash(o.before))} → ${esc(dash(o.after))} setups</span></li>`);
  const rejected = run.rejected.map((r) =>
    `<li class="opt rejected"><s>${esc(optionLabel(`${r.editKind}/${r.faceGroup}`))}</s> <span class="note">${esc(r.reason)}</span></li>`);
  return scored.length + rejected.length ? `<ul class="opts">${[...rejected, ...scored].join("")}</ul>` : "";
}

function sentences(run) {
  const s = sentenceSummary(run);
  if (!s.total) return "";
  const items = s.items.map((i) => `<li class="sent ${i.status}" title="${esc(i.reason)}">${
    i.status.startsWith("rejected") ? `<s>${esc(i.text ?? `Sentence ${i.index + 1}`)}</s>` : esc(i.text ?? `Sentence ${i.index + 1}`)}</li>`);
  const judged = s.items.some((i) => i.status === "verified" || i.status === "rejected-verify");
  const passed = s.items.filter((i) => i.status !== "rejected-check").length;
  const tally = judged ? `${s.verified} of ${s.total} sentences verified`
    : `${passed} of ${s.total} sentences passed the check · ${run.status === "live" ? "verifying…" : "not verified, so not published"}`;
  return `<div class="tally">${tally}</div><ul class="sents">${items.join("")}</ul>`;
}

function earlier(state, shown) {
  const rows = state.order.filter((id) => id !== shown.id).reverse().map((id) => {
    const r = state.runs[id];
    return `<li data-run="${esc(id)}">version ${esc((r.versionId ?? "").slice(0, 6))} · ${esc(statusText(r))}</li>`;
  });
  return `<div data-section="earlier">${rows.length
    ? `<details class="earlier"><summary>Earlier runs</summary><ul>${rows.join("")}</ul></details>` : ""}</div>`;
}

export function renderHTML(state, runId = null) {
  const run = (runId && state.runs[runId]) || latestRun(state);
  if (!run) return `<header class="hd"><span class="dot"></span>Anville</header><p class="empty">Waiting for the next save.</p>`;
  const cards = decisionCards(run);
  const reasons = Object.fromEntries(run.decisions.map((d) => [d.issueId, d.reason]));
  const calls = run.calls.length || run.liveCalls.length;
  // Top-level sections carry data-section, so panel.js replaces only the ones that changed.
  return `<header class="hd ${run.status}" data-section="header"><span class="dot"></span>Anville · version ${esc((run.versionId ?? "").slice(0, 6))}
  <span class="status">${esc(statusText(run))}</span></header>
<div data-section="ring">${ring(ringState(run))}</div>
<div data-section="cards">${cards.map((c) => (c.kind === "ask" ? askCard : card)(c, reasons[c.issueId])).join("")}
${costStrip(costSummary(run))}
${run.learned.length ? `<p class="learned">Learned from the shop: ${esc(run.learned.join(", "))}</p>` : ""}
${run.agentFailed ? `<p class="note">Agent failed; posted the setup count instead.</p>` : ""}</div>
<section class="agent" data-section="agent"><div class="tag">Agent${calls ? ` · ${run.calls.length || run.liveCalls.length} model calls` : ""}</div>
${rail(run)}
<ul class="chips">${chips(run)}</ul>
${options(run)}
${sentences(run)}
</section>
${earlier(state, run)}`;
}
