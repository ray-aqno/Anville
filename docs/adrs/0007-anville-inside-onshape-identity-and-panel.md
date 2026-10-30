# ADR 0007: Anville inside Onshape: its own identity, a docked panel, and live edits

- **Status:** Proposed
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
