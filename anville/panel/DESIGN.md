# Panel design (S9): the live ring, the decision card, the ask, the traveler

- **Status:** Draft for Rayyan's approval, 2026-10-29
- **Lane:** panel. It owns `anville/panel/` and `tests/panel/` only. Contract changes go through an ADR 0007 amendment.
- **Built against:** ADR 0007 (Contract, Ownership), ADR 0009 (stage graph), main at `bb535a2`, and `anville.setup_sheet/1` on `feat/shop-cost`.

## 1. Intent

The judges score Meta tooling (25%) and the demo (25%) by **watching**. The panel is where the agent is *seen* working, inside Onshape, next to the part.

The moment it exists for, in Rayyan's words: *you can see and feel the impact of the CAD and manufacturing recommendation, the forecast is clear, and you can move forward with a concrete decision.* The judge's "aha" is realising that the motto, **"Your home for manufacturing infrastructure"**, is the real thesis. Anville is one roof over a closed loop that crosses the design–shop wall. It is not a DFM checker.

So the panel has two jobs, in this order:
1. Show the loop and the decision. The **ring** shows where this run sits in the whole design ↔ shop loop. The **decision card** shows what changes, on which side of the wall, and by how much.
2. Show the evidence: the stages, every model call with its host, tokens and latency, and which of the agent's sentences were verified or rejected.

Rayyan's framing: a good frontend for a very robust product that's doing a lot on the backend. The panel must make the backend's work visible, and never overstate it.

## 2. Goals, in protected order

| Pri | Piece | By Nov 2 freeze |
|---|---|---|
| **P0** | Live run view: ring, decision card, stage rail, model-call chips, verified/rejected sentences. GET `/panel`, GET `/api/runs`, SSE `/api/stream`. | **Protected. The premise of the product.** |
| P1 | Designer points and asks: `requestSelection` → POST `/api/ask`; "Show in model" via `requestSelectionHighlight`. | Wanted for the video; cut before P0 slips. |
| P2 | The traveler (setup sheet page, phone-first) with the machinist's way back (POST `/api/shop-note`). | Cut first. |

## 3. Non-goals and anti-goals

- **Not a chat.** The panel never becomes a second place to converse. The ask is a pointer plus one question, and its answer is also posted as an Onshape comment on the face (ADR 0007 §4).
- **Not a dashboard as the hero.** No history charts or stats pages.
- **Never a faked number.** Every number on screen comes from a trace event's `detail`, or from the SetupSheet JSON. A field the trace lacks renders as "—", never as an estimate. This applies to dollars too: they stay "—" until the cost model's output reaches the trace. Placeholder fixtures are labelled as fixtures in tests and never shipped as data.
- **The panel lane makes no Onshape REST calls and no Modal deploys.** The panel uses Onshape's *client* messages (postMessage inside the iframe) only, and reads the store only through an injected reader.
- **Anti-goal:** the panel looks impressive while the run it shows is a fallback. Fallbacks and rejections are shown, in amber and struck through. They are evidence that the gate works.

## 4. Decided (with Rayyan, 2026-10-29)

1. **Layout "C": live, then the decision rises.**
   - **While a run is live:** the ring at the top, then the stage rail and the model-call chips filling in below it, with options appearing as they're scored.
   - **When the Decide stage lands:** the decision card slides in between the ring and the agent section. The agent section compacts but stays visible.
2. **Size: legible in a 1080p recording.**
   - Base 14 px text; the setup-count headline is about 34 px.
   - Designed for 440 px. It must still read at 320 px and use the space at 480 px.
   - *The right panel's real width is unknown*: Onshape's docs don't state it (verified absent, 2026-10-29). Measure it once the extension is registered.
3. **The ring:** one loop under one roof, split by the wall.
   - **Design side (left, blue):** "Saved vN".
   - **On the wall (Anville, dark):** "Reviewed" at the top; "Shop note → learned" at the bottom.
   - **Shop side (right, orange):** "Setup sheet".
   - **Edges:** "next part, informed" (learned → saved). The inner arc on the design side is "reply ⇄ re-plan", also used for "ask ⇄ re-plan".
   - **Rendering:** the current run's arc is lit, and the next stop pulses.
   - **On Part B,** the same ring opens with "learned" already lit. That's the learning beat.
4. **The decision card:** before → after setups, large. Under it, a **Design side | Shop side** split.
   - **Example (Part A):** design side "Weld chamfer **kept** · stated intent: weld prep"; shop side "Buy **910480** cutter · pays back in — parts".
   - **Below the split:** dollars per part, which stay "—" until present in the trace. Then a button: "Open setup sheet" in P0 (a disabled stub if there is no sheet); "Send setup sheet to shop" once P2 lands.
5. **Model chips:** one per model call: stage, model, a **host badge** (Baseten purple, Meta API blue, Fireworks reserved), tokens in/out, cached tokens, and latency. Fallbacks and retries show on the stage rail as an amber stage, and a retry as a small loop-back mark.
6. **Sentences:** "N of M sentences verified". Rejected sentences are struck through, with the reason on tap.
7. **The ask (P1):**
   - "◎ Point at a face" sends `requestSelection` (faces, 1 required). Onshape shows its own pick prompt.
   - The SELECTION message fills a face chip. The designer types a question, and the panel POSTs `/api/ask` with a client UUID `request_id` that is reused on retry.
   - The answer renders as **flag, then suggest** (Rayyan, 2026-10-29: never a bare ask).
   - "Show in model ↗" sends `requestSelectionHighlight` for any event's `face_ids`, including the rejected-edit rows.
8. **The traveler (P2):**
   - The machinist's phone page is rendered from `anville.setup_sheet/1`, labelled **predicted** until the shop runs it.
   - Two big buttons: "✓ Ran it as planned" and "Something was different…". The second opens a short note with quick tags: tool arrived, workholding changed, extra setup, outsourced op.
   - Either one POSTs a shop note into the event store. Main turns it into a ledger entry, and the ring's "learned" node lights up.
   - **Note shape (agreed with the shop lane, 2026-10-30; ADR 0007 Amendment 3, accepted):**
     - The fields: `{event: "shop.note", note_id (= client UUID request_id, key shop:<id>), sheet_id, outcome: as_planned|different, tools_arrived: [name], tags: [tool_arrived|workholding_changed|extra_setup|outsourced_op], setups_run: int|null, note_text (≤1000, verbatim), received_at}`.
     - The server copies `part` from the stored sheet; it never trusts the client for it.
     - The "arrived" checkbox appears only on tool rows the sheet marks `"owned": false` (shop lane: `setup_sheet(..., buying=(...))`).
     - The shop lane's `shop_note_entries(note, sheet=...)` makes each ticked catalog cutter a `tool_acquired`, with decision `shop_note:<note_id>`, on either outcome. Every note also becomes a `shop_note` lesson with its text verbatim. Nothing is read out of free text.
     - **Enforced by the shop lane** (after its Codex review, 2026-10-30):
       - a ticked name must be a sheet row with `"owned": false`; only a `back_chamfer` row naming a catalog cutter becomes `tool_acquired`;
       - `tools_arrived` and `tags` are lists of strings (`[]` when empty);
       - `setups_run` is an int ≥ 1 or null;
       - `note_text` is a string (`""` allowed for as_planned);
       - `note_id` and `sheet_id` are non-empty strings;
       - a missing or null optional field takes its default, and any other wrong type is refused.
     - P2's `/api/shop-note` validates to the same rules, so a bad note is a 422 at the panel, not a refusal deep in the ledger handler.

## 5. My calls (Rayyan can veto)

1. **The traveler doesn't use the panel token.** Its link lives in an Onshape comment that every collaborator sees, and the panel token `t` would open every allowlisted document. Each sheet instead gets a signed capability: `s = HMAC(panel_secret, "sheet:" + sheet_id)`, which grants that sheet's page and its shop note only.
2. **No front-end build step.** One HTML file, one JS module, one CSS file, served by the router, with inline SVG for the ring, no framework, and system fonts. That makes it fast to iterate and gives no CDN dependency inside Onshape's iframe.
3. **Derivation lives in a pure JS reducer** (`reduce(state, event) → state`), the single source of truth for what the panel shows. It's tested with `node --test` (Node 24 is on the dev machine, no packages needed). The Python side serves raw ADR 0007 events, unchanged.
4. **Burst pacing, as a backstop.** When events arrive in a burst (cursor gap, `ts` spread wider than the arrival spread), the client replays them at their **recorded** `ts` deltas, each gap clamped to 120 ms–1.2 s. Order and relative timing are real; only the wall-clock delay is shortened. Live events render immediately. If main flushes per stage (§8, ask 1), bursts become rare and this rarely runs.
5. **The SSE server polls the injected reader** about once a second per open stream, sends `id: <cursor>`, sends a heartbeat comment every 15 s, and closes the stream after about 55 s. `EventSource` reconnects with `after=<last cursor>`. This works whatever the platform's request time limit is (see §9).
6. **The panel shows the latest run**, plus a compact list of earlier runs for this document, which can be tapped to replay. A run is everything sharing one `run_id`.
7. **The ADR 0007 amendment** (proposed by me, accepted by Rayyan) adds:
   - the client messages `requestSelection` and `requestSelectionHighlight`;
   - `/sheet/{sheet_id}?s=` and POST `/api/shop-note`, with the signed-sheet auth;
   - the event key `shop:<request_id>`;
   - the per-call `latency_s` and the sentence `text` fields asked of main in §8.

8. **Decision card with more than one decision** (settled in the builder check):
   - One card per `propose` decision. The first proposed is the headline, and the others stack below it.
   - Before → after is that decision's option (`setups_before` → `setups_after`). Options are scored separately, so the panel never adds their effects together.
   - The design-side half reads:
     - "kept · <stated reason>" when the issue's group is protected;
     - the part edit, for a part-side option;
     - "no change" otherwise.
   - The shop-side half reads:
     - the shop option, for a shop-side option;
     - "no change" otherwise.
   - **No `ask` action** (main, 2026-10-29, on `feat/flag-and-suggest`): every surfaced issue is flag, then suggest.
     - For unknown intent, a `propose` decision can carry a `question`. That's the one conditional removal question, and it renders as a small line under the design-side half ("If the chamfer isn't needed: …?").
     - A `quiet` decision isn't shown on the card.
     - An unknown action value is ignored, not rendered.
9. **Smaller calls:**
   - The secret is env `ANVILLE_PANEL_TOKEN`, used for both `t` and the sheet HMAC key.
   - "Show in model" is hidden when an event has no `face_ids`.
   - Opened outside Onshape (no `server` query parameter), the panel skips `applicationInit` and runs read-only. That gives a clean backup recording surface.

## 6. Event → screen mapping (verified against `review.py`, `job.py` and `modal_app.py` at `bb535a2`)

| Screen element | Source event (kind · title · detail) | Exists today? |
|---|---|---|
| Ring "Saved vN" lit | `run_started` "Reviewing a new version" (`version_id`) | ✅ |
| Arc saved → reviewed animating | anything after `run_started` and before `run_finished` | ✅ |
| Ring "Reviewed" lit | `comment_posted` | ✅ |
| Ring "reply ⇄ re-plan" arc | a run triggered by a reply or ask | ❌ needs a `run_started.detail.trigger` (`version`, `reply`, `ask`) |
| Ring "Setup sheet" lit | a sheet link on the run | ❌ main |
| Ring "learned" lit | a shop-ledger event used in the run | ❌ main / shop |
| Stage rail | `detail.stage` ∈ facts, intent, plan, decide, check, verify, publish | ✅ |
| Pre-agent steps on the rail ("export", "geometry") | "Exporting STEP" and "Setup count computed from geometry" (no `stage`) | ✅ mapped by title + kind |
| Stage fallback (amber) / retry mark | `error` "Stage fell back to the fixed script" / "Model answer rejected" (`attempt`) | ✅ |
| Model chips | `verify` "Agent finished", `detail.calls[]` = {stage, model, host, prompt_tokens, completion_tokens, …usage} | ✅ except `latency_s` ❌ |
| Live chips during the run | `tool_call` "Model answered" (stage, tool, attempt) | ✅, but no model/host/tokens on it; the chip fills in at "Agent finished" |
| Options | `option` "Option scored" (key, side, setups_before, setups_after, changes_plan) | ✅; dollars ❌ |
| Rejected edits | `tool_result` "Edit rejected: the face's intent is stated" (edit_kind, face_group, reason) | ✅; `face_ids` ❌ (needed for highlight) |
| Decision card | `decision` "Decision made" (issue_id, action, option_key, reason, `question`) + the matching `option` + `verify` "Intent tagged…" (intents[group].reason) | ✅; `question` is landing on main's `feat/flag-and-suggest` |
| Sentences | `verify`/`error` "Sentence checked" (index, ok, reason); "Sentence verified" / "Sentence not supported" (index, reason) | ✅ but **no sentence text** ❌ |
| Run end | `run_finished` (or `error` "Review failed") | ✅ |

## 7. Architecture (panel side)

```
anville/panel/
  __init__.py        make_router(...), make_sheet_router(...), sheet_sig(secret, sheet_id)
  router.py          /panel, /api/runs, /api/stream, /api/ask
  sheet.py           P2: /sheet/{id}?s= (server-rendered HTML) and /api/shop-note?s= (ADR 0007 Amendment 3)
  auth.py            constant-time t check; HMAC sheet capability; 404 on any mismatch or non-allowlisted doc
  static/panel.html  shell; reads documentId/workspaceId/elementId/server/t from the URL
  static/panel.js    applicationInit + origin check; EventSource; reduce(); render(); pacing queue
  static/panel.css
  static/sheet.js    P2: the note's buttons (notePayload is pure, node-tested)
  static/sheet.css   P2: phone-first styles
tests/panel/
  test_router.py     auth, allowlist, cursor replay, SSE framing, ask idempotency (FastAPI TestClient)
  fixtures/*.json    trace dumps (real ones from main when they arrive; synthetic ones labelled synthetic)
  reducer.test.mjs   node --test: event sequences → expected view state (bursts, fallbacks, retries, verifier off)
```

- **`reader`** is a protocol: `trace_after(document_id, after, limit) -> list[dict]`. Main injects `StoreSvc().call.remote("trace_after", …)`; tests inject a list.
- **`writer`** is a protocol: `record(key, document_id, payload, ...) -> bool`. Main injects the store's `record`. The panel writes only `ask:<request_id>` and, in P2, `shop:<request_id>` events.
- **Onshape client messaging (verified in their docs, 2026-10-29):** send `applicationInit` first, with documentId, workspaceId and elementId; validate every incoming message's `origin` against the `server` query parameter; SELECTION then arrives on user selection. Right panels also support `requestSelection`, `requestSelectionHighlight`, `requestViewerImage`, `showMessageBubble` and `keepAlive`.

- **The flagged face's picture** (ADR 0007 Amendment 4): `anville/geometry/sam_anchor.py` draws it; the panel shows it under the cards (`/panel/render/{version_id}`) and the sheet above its setups (`/sheet/{id}/render`), both read through an optional `read_render` main injects. SAM 3.1's chip joins the agent's chips with its latency.

## 8. What the panel needs from main (handoff, in priority order)

| # | Ask | Why | Size (inference) |
|---|---|---|---|
| 1 | **Flush the trace at each stage boundary**, not once after the whole agent run (`modal_app.py:377-378`) | Otherwise every stage lands in one burst 12–21 s late, and the "live" ring isn't live | ~7 extra store round trips per run |
| 2 | **`latency_s` per call** in `Agent finished.calls[]` (already in the lane plan) | Latency is a chip field | small |
| 3 | **Sentence `text`** (slot-filled) on "Sentence checked" / "Sentence verified" | You can't show a verified sentence without its words | small |
| 4 | **Dollars on `option` events** once `cost_options` feeds Decide | Shows the forecast in $ | rides on S7 |
| 5 | **Mount the router** in `web()`: `api.include_router(make_router(...))` | Serving | small |
| 6 | **`face_ids` + `element_id`** on events that name a face group (rejected edits, issues, decisions) | "Show in model" highlight | small–medium (group → face IDs) |
| 7 | `run_started.detail.trigger` (`version` / `reply` / `ask`) | Ring inner arc | small |
| 8 | A **`panel.ask` handler** (P1): the same loop as a reply, answer posted on the face | The ask beat | medium |
| 9 | Sheet storage + id + signed link in the comment, plus a shop-note → ledger handler (P2) | Traveler | medium |

**Main's reply (2026-10-29):**
- **Taking now, in this order:** #1, #2, #3.
- **Waiting on others:** #4 after PR #20 merges; #5 once this lane's PR states the signature.
- **Later, in the S4/S6/S8 work:** #6–#9.
- **The ADR 0007 amendment** is drafted in this lane's PR and accepted by Rayyan.

Also asked of main: **a real trace dump** from a live Part A run (`trace_after` output as JSON) to use as test fixtures.
- **When:** main writes it to `~/.gstack/projects/ray-aqno-Anville/fixtures/` after Rayyan's next approved sandbox save.
- **Coverage:** a fallback can't be forced live, so the fallback and retry fixtures stay synthetic, built from the event shapes in `review.py` and `job.py`, and labelled synthetic.

## 9. Unverified; check before relying on it

- The right panel's width, and whether the user can resize it. *Not in the docs; measure.*
- SELECTION's fields (face IDs? deterministic IDs? microversion?) and `requestSelectionHighlight`'s `selections` format. *Not in the docs; probe live once the extension is registered.*
- How long a Modal web endpoint holds a streaming response open. *Prior: there's a per-request limit. The design (§5.5) doesn't depend on its value.*
- Whether Onshape's iframe allows our page to be framed (no `X-Frame-Options` / `frame-ancestors` from FastAPI by default: *prior*).
- `fastapi` and `httpx` aren't in the project's dev dependencies; they're only in the Modal image. Tests need them, which means editing `pyproject.toml` / `uv.lock` outside the panel's lane. **Needs Rayyan's OK.**

## 10. Left to the builder

- Exact colours, spacing and animation timing, within §4: blue design side, orange shop side, dark Anville, green done, amber fallback, Baseten purple, Meta blue.
- Ring geometry and label placement at 320–480 px.
- Copy for empty and error states ("Waiting for the next save", "Run ended with an error"), in Anville's plain voice.
- The order of the earlier-runs list and how many to keep (suggest 5).
