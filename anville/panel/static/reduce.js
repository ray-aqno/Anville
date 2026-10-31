// Trace events -> what the panel shows. Pure: no DOM, no clock, no network (tested with node --test).
// Every number shown comes from an event's detail; a missing field stays null and renders as "—".

export const STAGES = ["export", "geometry", "facts", "intent", "plan", "decide", "check", "verify", "publish"];

// Job steps that run before the agent carry no detail.stage; they are placed on the rail by title.
const JOB_STEPS = { "Exporting STEP": "export", "Setup count computed from geometry": "geometry" };

export function initialState() {
  return { runs: {}, order: [], cursor: 0 };
}

export function latestRun(state) {
  const id = state.order.at(-1);
  return id === undefined ? null : state.runs[id];
}

export function hostLabel(host) {
  if (!host) return "—";
  if (/baseten/i.test(host)) return "Baseten";
  if (/(^|\.)meta\.ai$/i.test(host)) return "Meta API";
  if (/fireworks/i.test(host)) return "Fireworks";
  return host;
}

function newRun(event) {
  return {
    id: event.run_id, versionId: event.version_id, status: "live", trigger: null,
    startedTs: event.ts, lastTs: event.ts, endedTs: null,
    stages: Object.fromEntries(STAGES.map((s) => [s, { status: "pending", retries: 0 }])),
    current: null, agentFailed: false, calls: [], liveCalls: [],
    issues: {}, intents: {}, options: {}, rejected: [], decisions: [], checks: [], verdicts: [],
    ledgerUsed: false, learned: [], costs: null, commentPosted: false, reviewFailed: false, anchor: null,
  };
}

function enterStage(run, stage) {
  if (run.current === stage) return;
  const prev = run.current && run.stages[run.current];
  if (prev && prev.status === "active") prev.status = "done";
  run.current = stage;
  if (run.stages[stage].status === "pending") run.stages[stage].status = "active";
}

function call(c) {
  const n = (v) => (typeof v === "number" ? v : null);
  return {
    stage: c.stage ?? null, model: c.model ?? null, host: c.host ?? null, hostLabel: hostLabel(c.host),
    promptTokens: n(c.prompt_tokens), completionTokens: n(c.completion_tokens),
    cachedTokens: n(c.cached_tokens ?? c.prompt_tokens_details?.cached_tokens), latencyS: n(c.latency_s),
    error: c.error ?? null,
  };
}

export function reduce(state, event) {
  if (!event || event.cursor <= state.cursor) return state;
  const next = structuredClone(state);
  next.cursor = event.cursor;
  let run = next.runs[event.run_id];
  if (!run) {
    run = next.runs[event.run_id] = newRun(event);
    next.order.push(event.run_id);
  }
  run.lastTs = event.ts;
  const d = event.detail ?? {};
  const stage = d.stage ?? JOB_STEPS[event.title];
  if (stage && run.stages[stage]) enterStage(run, stage);
  applyEvent(run, event, d);
  return next;
}

function applyEvent(run, event, d) {
  switch (event.title) {
    case "Reviewing a new version":
      run.trigger = d.trigger ?? null;
      return;
    case "Model called":  // emitted before the call (PR #24): the chip shows model and host while it runs
      run.liveCalls.push({ stage: d.stage, model: d.model ?? null, host: d.host ?? null, hostLabel: hostLabel(d.host),
        attempt: d.attempt ?? null, answered: false });
      return;
    case "Model answered": {
      const open = run.liveCalls.findLast((c) => c.stage === d.stage && c.attempt === d.attempt && !c.answered);
      if (open) open.answered = true;
      else run.liveCalls.push({ stage: d.stage, model: null, host: null, hostLabel: "—", attempt: d.attempt ?? null,
        answered: true });  // a trace from before "Model called" existed
      return;
    }
    case "Model answer rejected":
      if (run.stages[d.stage]) run.stages[d.stage].retries += 1;
      return;
    case "Stage fell back to the fixed script":
      if (run.stages[d.stage]) run.stages[d.stage].status = "fallback";
      return;
    case "No verifier: model prose is not published":
      run.stages.verify.status = "skipped";
      return;
    case "Agent failed; posting the setup count instead":
      run.agentFailed = true;
      return;
    case "Agent finished":
      run.calls = (d.calls ?? []).map(call);
      run.liveCalls = [];
      return;
    case "Face anchored":  // SAM 3.1 on the flagged face, after the comment posts (ADR 0007 Amendment 4)
      run.liveCalls = run.liveCalls.filter((c) => c.stage !== "anchor");
      if (typeof d.latency_s === "number") run.calls.push(call({ ...d, stage: "anchor" }));  // null: SAM wasn't called
      run.anchor = { group: d.group ?? null, iou: d.iou ?? null, agreed: d.agreed ?? null, error: d.error ?? null,
        versionId: d.version_id ?? run.versionId };
      return;
    case "Review failed":
      run.reviewFailed = true;
      return;
    case "Setup plan and issues computed":
      for (const i of d.issues ?? []) run.issues[i.id] = { kind: i.kind, faceGroup: i.face_group ?? null };
      return;
    case "Intent tagged and ask gate computed":
      run.intents = d.intents ?? {};
      return;
    case "Edit rejected: the face's intent is stated":
      run.rejected.push({ editKind: d.edit_kind, faceGroup: d.face_group ?? null, reason: d.reason ?? "",
        faceIds: event.face_ids ?? [] });
      return;
    case "Option scored":
      // The shop lane's Route fields arrive flat (main, PR #26); only plan-changing options are priced.
      run.options[d.key] = { key: d.key, name: d.name ?? null, editKind: d.edit_kind ?? null, params: d.params ?? {},
        side: d.side, before: d.setups_before ?? null, after: d.setups_after ?? null, changesPlan: !!d.changes_plan,
        cost: "one_time" in d ? { one_time: d.one_time ?? null, payback: d.payback ?? null, payback_mid: d.payback_mid ?? null,
          complete: d.complete ?? null, unpriced: d.unpriced ?? [] } : null };
      return;
    case "Costs compared":
      run.costs = { verdicts: d.verdicts ?? [], inputs: d.inputs ?? {} };
      return;
    case "Used what the shop learned":
      run.ledgerUsed = true;
      run.learned = Object.keys(d.tools ?? {});
      return;
    case "Decision made":
      run.decisions.push({ issueId: d.issue_id, action: d.action, optionKey: d.option_key ?? null,
        reason: d.reason ?? "", question: d.question ?? null });
      return;
    case "Sentence checked":
      run.checks[d.index] = { ok: !!d.ok, reason: d.reason ?? "", text: d.text ?? null };
      return;
    case "Sentence verified":
    case "Sentence not supported":
      run.verdicts[d.index] = { ok: event.title === "Sentence verified", reason: d.reason ?? "", text: d.text ?? null };
      return;
  }
  if (event.kind === "comment_posted") run.commentPosted = true;
  if (event.kind === "run_finished") finish(run, event);
}

function finish(run, event) {
  run.endedTs = event.ts;
  run.status = /^Superseded/.test(event.title) ? "superseded" : run.reviewFailed ? "error" : "done";
  for (const s of Object.values(run.stages)) if (s.status === "active") s.status = "done";
  run.current = null;
}

// --- derived views -------------------------------------------------------------

// An option in the comment's own words: the trace's `name` (review._name(), main PR #26), capitalized, plus the
// numbers from params. The panel never re-derives the name, so it can't drift from the comment.
export function optionText(option) {
  if (!option?.name) return optionLabel(option?.key);
  const p = option.params ?? {};
  let suffix = "";
  if (option.editKind === "set_min_radius" && p.radius != null) suffix = ` to ${p.radius} mm`;
  else if (option.editKind === "set_depth" && p.depth != null) suffix = ` to ${p.depth} mm`;
  else if (p.type === "back_chamfer" && p.angle != null) suffix = ` (${p.angle}°)`;
  return option.name.charAt(0).toUpperCase() + option.name.slice(1) + suffix;
}

// Fallback for traces older than `name`: plain words from the key, in the comment's wording.
export function optionLabel(key) {
  const p = String(key).split("/");
  const bc = p[0] === "add_tool" && /^BC([\d.]+)-(.+)$/.exec(p[3] ?? "");
  if (bc) return `Buy the ${bc[2]} back-chamfer cutter (${bc[1]}°)`;
  if (p[0] === "add_tool" && p[3]) return `Buy a longer-reach ${p[3]}`;
  if (p[0] === "route_machine" && p[1]) return `Send it to an outside ${p[1]}-axis shop`;
  if (p[0] === "remove_feature" && p[1]) return `Remove ${p[1]}`;
  if (p[0] === "set_min_radius" && p[2]) return `Open up ${p[1]}'s inside corner radius to ${p[2].replace(/^r/, "")} mm`;
  if (p[0] === "set_depth" && p[3]) return `Change ${p[1]}'s depth to ${p[3]} mm`;
  return String(key);
}

const NO_CHANGE = { text: "No change", note: null };

// One card per proposal, in decision order. Never adds options' effects together: each was scored alone.
export function decisionCards(run) {
  const cards = [];
  for (const d of run?.decisions ?? []) {
    // An ask comes only where no shop fix exists (main, PR #26): a flag with its question, no option.
    if (d.action === "ask" && d.question) {
      cards.push({ issueId: d.issueId, kind: "ask", question: d.question });
      continue;
    }
    const option = d.action === "propose" ? run.options[d.optionKey] : null;
    if (!option) continue;
    const group = run.issues[d.issueId]?.faceGroup ?? null;
    const intent = group ? run.intents[group] : null;
    const label = optionText(option);
    let design = NO_CHANGE;
    if (intent?.status === "protected") {
      design = { text: `${group} kept`, note: intent.reason ? `stated intent: ${intent.reason}` : "stated intent" };
    } else if (option.side === "part") {
      design = { text: label, note: null };
    }
    let shop = NO_CHANGE;
    if (option.side === "shop") {
      const buys = option.key.startsWith("add_tool/");
      shop = { text: label, note: buys ? paybackNote(option.cost) : null };
    }
    cards.push({ issueId: d.issueId, kind: "propose", optionKey: option.key, before: option.before, after: option.after,
      design, shop, question: d.question, cost: option.cost });
  }
  return cards;
}

// Mirrors the review's payback line: an end of the range that never pays off is said so, never filled in.
function paybackNote(cost) {
  if (!cost) return "pays back in — parts";
  if (!cost.payback) return "payback not claimed";  // a route that can't make the whole part
  const [lo, hi] = cost.payback;
  const mid = cost.payback_mid;
  if (lo == null) return "doesn't pay off at these inputs";
  if (mid == null) return `pays off past ${lo} parts at the best inputs only`;
  return `pays back from ~${mid} parts (${hi == null ? `${lo} at best; never at worst` : `${lo}–${hi}`})`;
}

// The cost verdict per quantity. Shows `recommended` (cheapest at every corner) or "depends", never `leader`:
// the leader can win only because an input is unpriced (main, PR #26). Placeholder inputs are named.
export function costSummary(run) {
  if (!run?.costs) return null;
  const route = (key) => (key === "current" ? "Keep the current plan" : optionText(run.options[key] ?? { key }));
  return {
    verdicts: run.costs.verdicts.map((v) => ({ quantity: v.quantity, text: v.recommended == null ? "depends" : route(v.recommended) })),
    placeholders: Object.entries(run.costs.inputs).filter(([, i]) => i?.status === "placeholder").map(([name]) => name),
  };
}

// Verify verdicts index the sentences that passed the check, in order, not the model's original indexes.
export function sentenceSummary(run) {
  const items = [];
  const passed = [];
  (run?.checks ?? []).forEach((c, index) => {
    if (!c) return;
    const item = { index, text: c.text, status: c.ok ? "unverified" : "rejected-check", reason: c.ok ? "" : c.reason };
    items.push(item);
    if (c.ok) passed.push(item);
  });
  (run?.verdicts ?? []).forEach((v, j) => {
    const item = v && passed[j];
    if (!item) return;
    item.status = v.ok ? "verified" : "rejected-verify";
    item.reason = v.ok ? "" : v.reason;
    item.text = item.text ?? v.text;
  });
  return { verified: items.filter((i) => i.status === "verified").length, total: items.length, items };
}

export function ringState(run) {
  const reviewed = !!run?.commentPosted;
  return {
    saved: run ? "lit" : "off",
    reviewing: !run ? "off" : run.status === "live" ? "active" : "lit",
    reviewed: reviewed ? "lit" : "off",
    setupSheet: reviewed ? "next" : "off",
    learned: run?.ledgerUsed ? "lit" : "off",
    innerArc: run?.trigger === "reply" || run?.trigger === "ask" ? "lit" : "off",
  };
}
