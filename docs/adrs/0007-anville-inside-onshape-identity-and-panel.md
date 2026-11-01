# ADR 0007: Anville inside Onshape: its own identity, a docked panel, and live edits

- **Status:** Accepted (Rayyan Aquino, 2026-10-30, by delegated review: still within the system's shape)
- **Date:** 2026-10-27
- **Depends on:** ADR 0001, ADR 0003, ADR 0006
- **Amends:** ADR 0005 (where shop and designer inputs can come from)

## Context

The webhook loop works, but from the designer's side an agent is invisible.

- **Attribution:** Every comment Anville posts is attributed to the owner of the API keys. Verified 2026-10-27: comments list shows `user: "Rayyan Aquino"` on Anville's own comments. A viewer can't tell what the agent did.
- **No sign of work in progress:** Between save and comment there is no visible activity, only a delay.

Onshape apps can render inside the Onshape UI (verified, onshape-public.github.io/docs/app-dev/extensions, 2026-10-27):

- **Locations:** an OAuth app can add an *element right panel*, an *element tab*, and *context-menu* actions on parts and features.
- **Unpublished apps:** an app works for its developer after registration, a store entry, and a subscription. Publishing is not required.
- **Messages from Onshape:** an embedded app receives `"SELECTION"` messages when the user selects entities (verified, client messaging docs).

Forum threads (dated, unverified) say apps can read but not set the camera, and can't read a selection made before the panel opened.

## Decision

1. **Anville gets its own Onshape account.** Its API keys are used for every comment, branch, version, and feature it creates. Each feature it adds is named `Anville · <change>`. The self-event filter (ADR 0003, F1) can then also drop events by that user.
2. **Edits are made in the open, on a branch.** A fix is applied through the REST feature API on an `anville/fix-<run>` workspace. A designer with the document open sees the feature appear and the part rebuild. *Live propagation to open sessions is an inference to verify in the fix-branch spike.*
3. **A docked panel (element right panel, OAuth app) shows the agent working.**
   - The panel is served by the same Modal app.
   - It streams the run trace: tool calls, measured numbers, options and their costs, the decision, the action, and the re-check.
   - The panel is a view onto the agent, never a second place to converse.
4. **The designer can point.** The panel forwards `"SELECTION"` (face IDs) with an optional question as a `panel.ask` event into the event store (ADR 0006). It goes through the same loop as a comment reply. **The answer is posted as an Onshape comment anchored to the selected face**, so the comment thread stays the record of the conversation, and the panel shows it live.
5. **No computer use for edits.** The model never clicks the CAD UI to change geometry; edits go through the API and are re-verified by the planner (ADR 0003). Driving the camera for screenshots is a possible later addition, not part of this decision.

### Contract between the agent and the panel

The agent emits trace events. The panel only reads them, plus one write endpoint. Both sides build against this; neither imports the other's internals.

**Trace event** (one JSON object; append-only per run):

```json
{
  "cursor": 1042,
  "run_id": "r_8f2c",
  "seq": 7,
  "ts": 1793072590.17,
  "document_id": "d58f…",
  "version_id": "d8a3…",
  "element_id": "d4ed…",
  "kind": "run_started | tool_call | tool_result | option | decision | action | verify | comment_posted | run_finished | error",
  "title": "Re-check: 3 → 2 setups",
  "detail": {},
  "face_ids": ["JLG"]
}
```

- `title` is the human-readable line the panel shows. `detail` holds the numbers. Any number in `title` must come from `detail`, never from the model's free text (ADR 0003 slot rule).
- `cursor` increases across all runs of a document; it is the event store's row order. `seq` orders events within one run. Replay always uses `cursor`, never `seq`.
- `face_ids` let the panel tie an event to geometry. When `face_ids` is non-empty, `element_id` is required, because a document can hold several Part Studios. Face IDs are valid only for `version_id` (or the microversion in `detail.microversion_id` for workspace runs).

**Endpoints** (on the existing Modal web app; all scoped to allowlisted documents):

| Method | Path | Purpose |
|---|---|---|
| GET | `/panel?documentId=&workspaceId=&elementId=&t=<panel token>` | Serves the panel page. Onshape loads it in the right panel. |
| GET | `/api/runs?documentId=` | Recent runs with their events, for first paint. |
| GET | `/api/stream?documentId=&after=<cursor>` | Server-sent events of trace events with `cursor > after`, across runs. The panel resumes with the last cursor it rendered. |
| POST | `/api/ask` `{request_id, documentId, workspaceId, elementId, microversion_id?, face_ids, question}` | Inserts a `panel.ask` event (ADR 0006 delivery order). Returns 202 with its event id, or 200 with the original event id on a duplicate. |

- **Auth for the hackathon:**
  - The panel token arrives in the extension URL as `t`.
  - The panel sends it back as the `t` query parameter on **every** `/api/*` request, including the SSE stream. `EventSource` can't set headers, and third-party cookies inside Onshape's iframe aren't reliable.
  - The server checks `t` in constant time on every `/panel` and `/api/*` request (same pattern as the webhook secret, F2). It returns 404 on a mismatch, and 404 for any document not on the allowlist.
  - Known cost: the token appears in request logs, like the webhook secret. Rotate both before the demo.
- **`/api/ask` idempotency:**
  - `request_id` is a client-generated UUID, reused on retries. The event store key is `ask:<request_id>` (ADR 0006).
- **Selection versions:**
  - The panel sends the selection's `microversion_id` when the client provides it.
  - Otherwise the server records the workspace's current microversion when the event is inserted.
  - The answering job maps face IDs from that microversion to the version it analyzes with `idtranslations`. A face that can't be mapped is answered as "that face no longer exists", never guessed.
- **Later:** Onshape OAuth session validation replaces the token. This is noted as a known limitation.

### Ownership

- **Agent side:** `anville/trace.py` (emitter and storage), event store, and Modal deployment.
- **Panel side:** `anville/panel/` (static page, a FastAPI `router` mounted by `modal_app.py`).
- Changes to the contract above go through an amendment to this ADR.

## Options considered

1. **Comments only (status quo).** Reliable, but the agent stays invisible between save and comment, and is mis-attributed.
2. **Standalone web dashboard.** Easy to build, but it pulls the designer out of the CAD tool, and the brief treats it as a fallback, never the hero.
3. **Identity + live edits + docked panel (chosen).**
4. **Full computer use** (the model drives the Onshape UI from screenshots). Most literal autonomy, but there's no Meta model built for computer use, every step costs a model round trip, and click-edits bypass the planner's re-verification.

## Trade-offs

- **Setup steps outside the repo:** a second Onshape seat, an OAuth app registration, a store entry, and a subscription.
- **A public read surface:** the panel adds read endpoints. They're limited to allowlisted documents and protected by a token, with no OAuth for now.
- **A second API key pair counts against the same enterprise call pool.**

## Consequences

- **Easier:** the demo shows the agent acting under its own name, inside the designer's tool. Selection gives the designer a way to point that a chatbot can't offer.
- **Harder:** another surface to keep in sync with the trace schema. OAuth app maintenance.
- **Revisit:** if the fix-branch spike shows API edits don't appear live in open sessions, the panel's "action" events carry the visible proof instead, with a link to the branch. Also revisit if Onshape exposes camera control to apps.

## Amendment 1 (Accepted by Rayyan Aquino, 2026-10-30): Anville's identity through OAuth

**Context.**
- The Anville account is on Onshape's free plan, which can't create API keys (Rayyan, 2026-10-29). It can register an OAuth application.
- OAuth tokens act "on behalf of the users of the application", while API keys act only as the user who generated them (Onshape developer docs).
- **Verified 2026-10-29** on onshape-public.github.io/docs/auth/oauth:
  - the authorize and token endpoints;
  - access tokens last 60 minutes;
  - **every refresh returns a new refresh token.**
- **Correction:** the identity was never needed for reply → re-plan. Anville recognises its own events by the IDs it created (`events.py`, F1), not by user.

**Decision.**
1. **The Anville account registers the OAuth app** (scopes: read profile, read, write), and grants it once through `/oauth/start`. `/oauth/callback` accepts only a `state` that `/oauth/start` issued, once, within 10 minutes. Two more checks (Codex review of the OAuth PR, 2026-10-30):
   - **The flow has its own key,** `ANVILLE_OAUTH_ADMIN_TOKEN`, not the webhook secret. Anyone who can read the webhook URL holds that secret, so it can't also authorize swapping the posting identity. The flow is closed when the key is unset or equal to the webhook secret. Rotate the key after the grant, since it travels in a query string.
   - **Only the configured account is saved.** The callback reads `/users/sessioninfo` with the new token and saves the grant only if its email matches `ANVILLE_ACCOUNT_EMAIL`. Otherwise it refuses (403), including when the email is missing.
   - **The save goes through the keeper's lock,** so an in-flight refresh of the old grant can't overwrite the new one.
2. **Only comments go out on the OAuth token,** and so carry Anville's name. Reads, STEP exports and webhooks stay on the owner's API keys. An app's calls count against its owner's API limits, here the free account's, so this keeps that usage to one call per review. Fix-branch edits will follow the same rule when they're built.
3. **The grant lives in the store** (`oauth_grant`, one row), saved durably on every refresh.
   - The **store service** refreshes it, under one lock in its single container (`GrantKeeper`).
   - Rotation means two concurrent refreshes would leave one job holding a dead token.
   - Callers get an access token (`oauth_token()`), never the refresh token.
4. **Fallback:** with no grant, a failed refresh, or a 401/403 from Onshape, the comment posts on the owner's keys with an `Anville:` header. Any other error is raised and not retried, so a comment is never posted twice. The trace records `posted_as`.

**Trade-offs.**
- The store service now holds the OAuth client secret, and makes one outbound call per refresh.
- The redirect URL is fixed to production, so staging can't run its own grant: a second grant could revoke the first.

**Revisit:**
- if the free plan's API limits turn out too small for comment volume;
- if the panel needs per-designer OAuth (ADR 0007's "Later").

## Amendment 2 (Accepted, Rayyan Aquino, 2026-10-30): panel contract details

Drafted by the panel lane against `anville/panel/DESIGN.md`. Main agreed the panel lane drafts it (2026-10-29). Only Rayyan accepts it. Each claim is labelled **verified**, **prior** or **inference**.

**1. Onshape client messages** (verified: onshape-public.github.io/docs/app-dev/messages/element-right-panel/, read 2026-10-29).
- The panel sends `applicationInit` first, with `documentId`, `workspaceId` and `elementId`.
- It accepts an incoming message only when `event.origin` equals the `server` query parameter Onshape passed in the extension URL.
- After a valid `applicationInit`, Onshape sends `SELECTION` messages on user selection.
- Right panels also support:
  - `requestSelection` (the user picks entities, with a count and a type filter);
  - `requestSelectionHighlight` (highlights entities in the viewport);
  - `requestViewerImage`, `showMessageBubble` and `keepAlive`.
- **Not documented:** the fields of `SELECTION`, and the `selections` format for `requestSelectionHighlight`. They get probed live once the extension is registered.
- **Supersedes:** the forum note in Context says apps can't read a selection made before the panel opened. That doesn't matter now: the panel asks for a fresh pick with `requestSelection`.

**2. Trace additions (agent side).** Every field is optional, so the panel renders "—" when one is missing and never estimates. Main's PR #24 adds the first three rows below, and per-stage flushing. Main's PR #26 (stacked on #24) adds the cost, "Costs compared" and "Used what the shop learned" rows, and brings back `ask`.

| Event | Field | Shape |
|---|---|---|
| `tool_call` "Model called" (new; main's PR #24) | `stage`, `model`, `host`, `attempt` | emitted **before** each model call, and the trace flushes right after it, so it arrives live. It is the panel's stage-start signal: the chip shows the model and host while the call runs |
| `verify` "Agent finished" | `calls[].latency_s`; `calls[].error` | float, seconds, per model call (main's PR #24). A failed call (`ModelUnavailable`, timeouts included) has `latency_s` and `error` and no token counts |
| "Sentence checked", "Sentence verified", "Sentence not supported" | `text` | the sentence after slot filling, exactly as the designer would read it |
| `decision` "Decision made" | `action`, `question` | a proposal may carry one question (PR #23). Action `ask` (no `option_key`) comes only where no shop-side fix exists (PR #26); the panel shows it as a flag card with its question |
| `option` "Option scored" (plan-changing options only) | `one_time`, `costs`, `payback`, `payback_mid`, `complete`, `unpriced` | flat in `detail`, the shop lane's `Route` field names: `one_time` [lo, hi]; `costs` {quantity: [lo, hi]}; `payback` [lo or null, hi or null], or null when the route can't make the whole part; `payback_mid` int or null. The panel never fills in an end of the payback range that never pays off |
| `option` "Option scored" | `name`, `edit_kind`, `params` | `name` is the comment's own wording for the fix (review `_name()`, slots filled, lower case); `params` holds the option's numbers (`radius`, `depth`, `angle`, `owned`, ...). The panel capitalizes `name` and adds the numbers, so the panel and the comment can't drift. A trace without `name` falls back to plain words from the key (main's PR #26) |
| `decision` "Costs compared" (stage plan) | `verdicts`, `inputs`, `scenarios` | `verdicts` [{quantity, recommended, leader, flip}]; `inputs` {name: {lo, hi, unit, status, source}}. The panel shows `recommended`, or "depends" when it is null, and names every input whose status is `placeholder`. It never headlines `leader`: that can win only because an input is unpriced. Ranking waits on ADR 0010 |
| `run_started` | `detail.trigger` | `version`, `reply` or `ask` |
| any event that names a face group | `face_ids` + `element_id` | per the original contract; lets the panel highlight the face |
| `tool_result` "Used what the shop learned" (PR #26) | `tools` | {name: {review, decision, option, on}}. Replaces the `ledger_used` field proposed here earlier. The panel lights its "learned" node and names the tools |

**Verify indexes** count the sentences that passed the check, not the model's original sentence indexes (verified, `review.py`, `verify_sentences`). The panel maps them back; the agent side doesn't need to change.

**3. Endpoints.**
- `GET /panel/assets/{name}` serves the panel's static code from a fixed allowlist of file names, with no token. It holds no data, and the repo is public.
- `GET /api/runs?documentId=&t=` returns `{"runs": [{"run_id", "events": [...]}], "cursor": int}`:
  - at most the latest 5 runs, oldest first;
  - `cursor` is the highest cursor returned, or 0.
  - The server pages through `trace_after` to reach the latest runs. *Inference:* a `trace_tail` store op would save that paging on long documents, but it isn't needed at hackathon volumes.
- `GET /api/stream?documentId=&after=&t=` uses this framing:
  - `retry: 1000` first;
  - per event: `id: <cursor>`, `event: trace`, `data: <event JSON>`;
  - the comment `: hb` as a heartbeat;
  - the server closes after about 55 s, and the browser reconnects.
  - The resume point is the greater of `after` and the `Last-Event-ID` header, because `EventSource` reconnects with its original URL.
- `POST /api/ask` stores `microversion_id` as sent, or null.
  - **Change from the original contract:** recording the workspace's current microversion moves from insert time to the job that answers the ask, because the panel lane makes no Onshape REST calls.
  - The stored payload is the request body plus `"event": "panel.ask"` and `received_at`.
  - It returns 202 `{"event_id": "ask:<request_id>"}` when inserted, or 200 with the same body on a duplicate.

**4. Auth.**
- The panel token is env `ANVILLE_PANEL_TOKEN`. It's checked in constant time on `/panel` and every `/api/*`, and a mismatch or a document off the allowlist returns 404.
- The setup sheet page (P2) never uses `t`, because a link in an Onshape comment is visible to every collaborator on the document. It will use a per-sheet capability, `HMAC(ANVILLE_PANEL_TOKEN, "sheet:" + sheet_id)`, valid for that sheet and its shop note only.
- The P2 endpoints are specified in a later amendment, when P2 is built.

**Consequences.**
- **Easier:** both lanes build against named fields, and the panel degrades to "—" while main adds them one at a time.
- **Harder:** a seven-field extension that the panel's synthetic fixtures must track until a real trace dump replaces them.

## Amendment 3 (Accepted, Rayyan Aquino, 2026-10-30): the traveler's endpoints

**Context.** Amendment 2 left the setup sheet page (P2) for a later amendment. This is that amendment. The note shape was agreed with the shop lane on 2026-10-30 (`anville/panel/DESIGN.md` §4.8), and main stores sheets and turns notes into ledger entries.

**1. Endpoints** (`anville/panel/sheet.py`, a second router: `make_sheet_router(read_sheet, writer, *, panel_secret)`):

| Method | Path | Behaviour |
|---|---|---|
| GET | `/sheet/{sheet_id}?s=` | Renders the stored `anville.setup_sheet/1` as one phone-first HTML page, labelled **Predicted** while `basis` is `predicted`. Each tool row with `"owned": false` gets an "Arrived" checkbox. Headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`, because the link carries the capability |
| POST | `/api/shop-note?s=` `{request_id, sheet_id, outcome, tools_arrived?, tags?, setups_run?, note_text?}` | Records a `shop.note` event keyed `shop:<request_id>`, on the sheet's document. Returns 202 `{"event_id"}`, or 200 with the same body on a duplicate |

- `read_sheet(sheet_id) -> (document_id, sheet) | None`. Main backs it with sheets stored as events keyed `sheet:<id>` (the existing `event` store op).
- The stored note is `{event: "shop.note", note_id (= request_id), sheet_id, part, outcome, tools_arrived, tags, setups_run, note_text, received_at}`. `part` is copied from the stored sheet's `part.source_sha256`. A client that sends `part` gets a 422.

**2. Auth.**
- `s = HMAC-SHA256(ANVILLE_PANEL_TOKEN, "sheet:" + sheet_id)`, as lowercase hex. `anville.panel.sheet_sig` computes it for the link main posts in the comment.
- `sheet_id` must match `[A-Za-z0-9_-]{1,64}`.
- The signature is checked in constant time **before** any store read. A bad signature, another sheet's signature, or an unknown sheet returns an empty 404. On a POST this happens before body validation, so a bad link never learns the route from a 422.
- `/api/shop-note` is the one `/api/*` route that takes `s` instead of `t` (Amendment 2 §4).

**3. Validation** (the shop lane's `shop_note_entries` rules, so a bad note is a 422 here, not a refusal in the ledger handler):
- Strict types, unknown fields refused.
- `outcome` is `as_planned` or `different`. A `different` note needs non-blank `note_text` (at most 1000 characters, stored verbatim).
- `tools_arrived` is at most 20 names, each a tool row on this sheet with `"owned": false`.
- `tags` is a subset of `tool_arrived`, `workholding_changed`, `extra_setup`, `outsourced_op`.
- `setups_run` is an int from 1 to 99, or null.
- A missing or null optional field takes its default (`[]`, `[]`, null, `""`).

**Consequences.**
- **Easier:** the machinist's answer reaches the ledger with no account and no app. The link alone is enough, and it opens exactly one sheet.
- **Harder:** anyone holding a sheet link can post notes to that sheet until the panel secret is rotated. Rotating the secret also breaks every sheet link already posted.
- **Revisit:** whether a note should expire with the sheet, after a newer version's sheet replaces it.

## Amendment 4 (Accepted, Rayyan Aquino, 2026-10-31): the flagged face's picture, traced by SAM 3.1

**Context.** Locked decision 5 gives SAM 3.1 one job: point the flag at exact geometry. ADR 0004 puts `sam-3.1` on the Meta Model API. RFC 0003 says SAM never chooses the face. `anville/geometry/render.py` (RFC 0004) already paints the flagged group exactly, from the B-rep. Rayyan decided on 2026-10-31 that SAM's role is to **confirm the code**:
- the code picks and paints the group;
- SAM traces it on a plain render of the same view;
- SAM's outline is drawn only when it agrees.

**1. What runs** (`anville/geometry/sam_anchor.py`, `anchor(step_path, facts, group, *, sam) -> Anchor | None`):
- The highlighted render picks the view. A plain render from the same camera goes to SAM, with the group's largest visible piece as a box prompt and a positive point on its pixel nearest the box centre (`meta-sam-parser`'s `build_object_prompt`).
- SAM's mask is compared with the code's pixels (render.py's own key, R > B, inside that box).
  - At IoU ≥ 0.8, its outline is drawn in blue over the code's orange.
  - Below that, the picture is the code's alone, and the record says SAM disagreed.
  - A label chip with the group's name (`G11`) sits just above the group, over the prompted point.
- SAM never raises out of `anchor()`. A timeout, an HTTP error, an unreadable reply or no `MODEL_API_KEY` yields `agreed: null` and an `error`, and the picture still goes out.
- Verified 2026-10-31, on synthetic `demo_bracket`:
  - The probe sent `POST https://api.meta.ai/v1/responses` with stream off and the image as a base64 `data:` URL, and got HTTP 200.
  - Masks decode with Meta's `meta-sam-parser==0.1.0` (SAM License; Rayyan approved the pinned dependency).
  - Live IoU was 0.99 (G1), 0.857 (G3, a chamfer stripe) and 0.918 (G9, a hole), at 0.6–1.9 s per call.

**2. Trace.** `anchor_fn(version_id, group)` runs after the comment posts, on the same `run_id`. Main owns it.
- It emits `tool_call` "Model called" with `{stage: "anchor", model: "sam-3.1", host: "api.meta.ai"}`, but only when SAM is configured.
- It then emits `tool_result` **"Face anchored"** with `{group, model, host, latency_s, iou, agreed, error, version_id}`. `latency_s` is null when SAM wasn't called.
- The PNG is written to `/data/render/{version_id}.png` **before** "Face anchored" is emitted, so the panel never asks for a picture that isn't there yet.
- The panel adds SAM's chip (latency only; SAM has no tokens). It shows the picture under the decision cards, with a caption saying whether SAM's trace matched, didn't match, or didn't come.

**3. Endpoints.** Both routers take an optional `read_render(version_id) -> bytes | None`. Without it, both routes return 404.

| Method | Path | Behaviour |
|---|---|---|
| GET | `/panel/render/{version_id}?documentId=&t=` | The PNG, behind the panel token and the document allowlist. `Cache-Control: no-store` |
| GET | `/sheet/{sheet_id}/render?s=` | The PNG for the stored sheet's `version_id`, behind the sheet's signature. Amendment 3's private headers |

- `version_id` must match `[A-Za-z0-9_-]{1,64}` before it reaches `read_render`, because main maps it to a file path.
- The sheet page shows the picture above the setups only when it exists at page load.

**Consequences.**
- **Easier:** judges see which face a flag means, and see Meta's segmentation model checking the geometry with a number, not decoration. The picture needs no Onshape call, since comments link to the sheet.
- **Harder:**
  - One more paid call per review (about $0.0025; ADR 0004's price).
  - One more dependency.
  - The panel token can fetch the picture for any stored version id, not only its own document's. Version ids are Onshape's 24-character ids, not secrets, so a token holder for one allowed document could read another allowed document's picture.
- **Revisit:** checking the version's document in `read_render`. Also whether to show SAM's trace when it disagrees, as evidence for a human.

## Amendment 5 (Accepted, Rayyan Aquino, 2026-10-31): the card's "Open setup sheet" link

**Context.** The decision card's "Open setup sheet" button was rendered but never enabled. Rayyan hit this live: "I can't even open the setup sheet from the panel." The sheet's link is signed with the panel secret (Amendment 3), and the panel page must never hold that secret, so the page can't build the link itself.

**Decision.**
- Main adds `sheet_url` to the existing `tool_result` **"Setup sheet ready"** event: `{sheet_id, setups, buying, sheet_url}`.
  - It's the same absolute signed link the review comment carries (`deploy.sheet_link`).
  - It's null when no panel token is set.
- The reducer keeps `run.sheet = {id, url, setups}`. The card renders `<a class="sheet" href=… target="_blank" rel="noopener noreferrer">`, but only when the link matches `https://<host>/sheet/<id>?s=<64 lowercase hex>`. Otherwise the disabled button stays, so no `javascript:` or look-alike link from a trace is ever clickable.
- Runs recorded before this change have no `sheet_url` and keep the disabled button.

**Options considered.** A `/panel/sheet/{id}` route that signs and redirects. Rejected: one allowed document's panel token could then mint links to another document's sheets. The trace is read per document, so a link in it reaches only that document's panel, and the same link is already in the comment every collaborator sees.

**Consequences.**
- **Easier:** the designer and the demo reach the traveler from the card, in one click.
- **Harder:** the signed link now also sits in the trace store, so rotating the panel secret leaves dead links in old runs, as it does in old comments.
- **Revisit:** whether Onshape's right-panel iframe allows a new tab (`allow-popups`). Unverified until it's clicked docked. In a plain browser tab it works.
