# Panel P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A docked Onshape panel that shows one Anville review live: the loop ring, the decision card, and the agent's work (stage rail, model-call chips, verified and rejected sentences). It ships as a FastAPI `router` that main mounts, with the ADR 0007 contract endpoints `/panel`, `/api/runs`, `/api/stream` and `/api/ask`.

**Architecture:**
- **Python serves only.** It checks the token and the allowlist, reads raw ADR 0007 trace events through an injected `reader`, streams them as SSE, and writes `panel.ask` events through an injected `writer`.
- **Everything the panel shows comes from pure JS modules:** `reduce.js` (events → view state) and `pace.js` (burst replay timing). Both are tested with `node --test`.
- **`view.js` renders that state** to the DOM, and `panel.js` wires the URL, the fetch and the EventSource together. There's no build step.

**Tech Stack:** Python 3.12, FastAPI 0.162 (`APIRouter`, `StreamingResponse`), pytest + FastAPI `TestClient` (httpx), vanilla ES modules, inline SVG, Node 24 `node --test`.

**Spec:** `anville/panel/DESIGN.md` (approved by Rayyan, 2026-10-29). ADR 0007 (Contract, Ownership), ADR 0009.

## Global Constraints

- **Files:**
  - You edit only `anville/panel/**` and `tests/panel/**`, plus the ADR 0007 amendment section in `docs/adrs/0007-anville-inside-onshape-identity-and-panel.md` (Task 1).
  - Never edit `modal_app.py`, `anville/agent/**` or `anville/store/**`.
  - Never commit `CLAUDE.md`.
- **No Onshape REST calls, no Modal deploys, no direct store reads.** Data comes only through the injected `reader` and `writer`.
- **Commits:** only with Rayyan's go. Small, commented commits on `feat/onshape-panel`, ending with the session's attribution lines.
- **Every number on screen comes from an event's `detail`.** A missing field renders `—`, never an estimate. Fixtures that aren't from a live run are named `*.synthetic.json` and say so in a top-level `"note"`.
- **Auth:**
  - The secret is env `ANVILLE_PANEL_TOKEN`, passed in as `panel_secret`.
  - `t` is a query parameter on `/panel` and every `/api/*` request, compared in constant time with `anville.connectors.onshape.events.token_matches`.
  - A bad `t`, a missing `t`, or a `documentId` not on the allowlist all return **404** with an empty body.
- **Size:** designed for 440 px wide; must read at 320 px; base text 14 px; the setup-count headline 34 px.
- **Colours** (CSS custom properties in `panel.css`):

  | Property | Value | Use |
  |---|---|---|
  | `--design` | `#1d4ed8` | design side |
  | `--shop` | `#c2410c` | shop side |
  | `--anville` | `#1d2433` | Anville |
  | `--done` | `#2f7d4f` | done |
  | `--fallback` | `#b7791f` | fallback |
  | `--baseten` | `#5b3fc4` | Baseten badge |
  | `--meta` | `#1d4ed8` | Meta API badge |
  | `--fireworks` | `#d9480f` | Fireworks badge |
  | `--muted` | `#6b7383` | muted text |
- **Stage rail order:** `export, geometry, facts, intent, plan, decide, check, verify, publish`.
- **Pacing:** each gap is clamped to `[120 ms, 1200 ms]`. Events count as one burst when they arrive within `250 ms` of each other.
- **SSE timing** (all keyword arguments of `make_router`, so tests can shrink them): poll the reader every `1.0 s`, send a heartbeat every `15 s`, close the stream after `55 s`.

## Review Focus

1. **Model free text is HTML-escaped everywhere.** A decision `reason` containing `<img src=x onerror=alert(1)>` renders as literal text. Test in Task 7 (`view.test.mjs`).
2. **EventSource auto-reconnect resends the *original* URL.** The server must resume from `Last-Event-ID` when it is greater than `after`, or the panel replays old events. Test in Task 3.
3. **Verify-stage indexes are positions among the sentences that passed the check, not the model's original indexes.** Verify index 1 points to the 2nd sentence that passed. Test in Task 5.
4. **A run that ends without the agent** (the agent is off, "Agent failed…", or superseded) still shows a finished ring and a plain status line, never a frozen "● live". Test in Task 4.
5. **A document with more than 500 events.** `/api/runs` must page past the first 500 and return the *latest* runs, not the oldest. Test in Task 2.

---

### Task 1: ADR 0007 Amendment 1 (contract first)

**Files:**
- Modify: `docs/adrs/0007-anville-inside-onshape-identity-and-panel.md`. Append a section `## Amendment 1 (2026-10-30): panel contract details`, with **Status: Proposed**. An agent never marks it Accepted.

**Interfaces:**
- Produces: the field names that Tasks 2–7 and main's asks rely on.

- [ ] **Step 1: Write the amendment.** It has these subsections, each stating its source and its verified/prior/inference label:
  1. **Client messages** (verified, onshape-public.github.io/docs/app-dev/messages/element-right-panel/, 2026-10-29).
     - Send `applicationInit` first, with documentId, workspaceId and elementId.
     - Validate each incoming message's `origin` against the `server` query parameter.
     - P1 uses `requestSelection` and `requestSelectionHighlight`.
     - SELECTION's fields are undocumented; probe them live.
  2. **Trace additions (agent side):**
     - `calls[].latency_s` (float, seconds).
     - `text` on "Sentence checked", "Sentence verified" and "Sentence not supported" (slot-filled).
     - `question` on "Decision made" (null unless set).
     - `cost` on "Option scored", optional: `{one_time: [lo, hi], payback: [lo, hi|null] | null, payback_mid: int|null, unpriced: [str]}`. These are the shop lane's `Route` field names.
     - `run_started.detail.trigger` ∈ `version | reply | ask`.
     - `face_ids` + `element_id` on events that name a face group.
     - `ledger_used: [{id, kind}]` on any event where a review applies a ledger entry. The panel lights "learned" when any event in the run has a non-empty `ledger_used`.
  3. **Endpoints:**
     - `GET /panel/assets/{name}`: static code, no token, served from an allowlist of file names. It holds no data.
     - `GET /api/runs` returns `{"runs": [{"run_id", "events": [...]}], "cursor": int}`: at most the last 5 runs, oldest first, where `cursor` is the highest cursor returned (0 if none).
     - SSE framing: `retry: 1000`, then per event `id: <cursor>`, `event: trace`, `data: <event JSON>`. A heartbeat is the comment line `: hb`. The server closes after about 55 s. The resume point is max(`after`, `Last-Event-ID`).
     - `POST /api/ask` stores `microversion_id` as sent, or null. **Change from ADR 0007:** mapping it to the workspace's current microversion moves from insert time to the answering job, because the panel lane makes no Onshape REST calls.
  4. **Auth:** env `ANVILLE_PANEL_TOKEN`. The traveler (P2) will use a per-sheet `HMAC(ANVILLE_PANEL_TOKEN, "sheet:" + sheet_id)` capability, never `t`, because comment links are visible to every collaborator. The P2 endpoints are specified when P2 is built.

- [ ] **Step 2: Commit** (with Rayyan's go): `git add docs/adrs/0007-*.md && git commit -m "docs(adr-0007): propose Amendment 1, panel contract details"`

---

### Task 2: Router, auth, `/panel`, assets, `/api/runs`, fixtures

**Files:**
- Create: `anville/panel/__init__.py`, `anville/panel/router.py`, `anville/panel/static/panel.html` (a stub with `<div id="app">` and the module script `assets/panel.js`), `tests/panel/conftest.py`, `tests/panel/test_router.py`
- Create: `tests/panel/fixtures/part_a_live.synthetic.json`, `tests/panel/fixtures/part_a_fallback.synthetic.json`

**Interfaces:**
- Produces, in `anville/panel/router.py`, re-exported from `anville/panel/__init__.py`:
  ```python
  Reader = Callable[[str, int, int], list[dict]]          # trace_after(document_id, after, limit)
  Writer = Callable[..., bool]                            # record(key, document_id, payload, version_id=None, version_ts=None)
  def make_router(reader: Reader, writer: Writer, *, panel_secret: str, allowed_docs: Callable[[], set[str]],
                  poll_s: float = 1.0, heartbeat_s: float = 15.0, stream_s: float = 55.0,
                  clock: Callable[[], float] = time.time) -> APIRouter
  ASSETS = frozenset({"panel.js", "panel.css", "reduce.js", "pace.js", "view.js"})
  RUNS_KEPT = 5; PAGE = 500; MAX_PAGES = 40
  ```
- Produces fixtures: a JSON object `{"note": "...synthetic...", "document_id": "dSYN", "events": [...]}` in ADR 0007 shape (`cursor, run_id, seq, ts, document_id, version_id, element_id, kind, title, detail, face_ids`).
  - **`part_a_live`, run `v7aaaaaa-1`**, events in this order, with `ts` increasing as noted:
    1. `run_started` "Reviewing a new version" (`trigger: "version"`)
    2. `tool_call` "Exporting STEP"
    3. `verify` "Setup count computed from geometry" (`setups: 2`)
    4. `verify` "Setup plan and issues computed" (stage facts; `setups: 2`; issues `[{id:"I1", kind:"forces_setup", face_group:"G5"}]`)
    5. `verify` "Intent tagged and ask gate computed" (stage intent; intents `{"G5": {status:"protected", reason:"weld prep"}}`)
    6. `tool_call` "Model answered" (stage plan, attempt 1); +6.1 s
    7. `decision` "Issues chosen" (selected `["I1"]`)
    8. `tool_result` "Edit rejected: the face's intent is stated" (edit_kind `remove_feature`, face_group `G5`, reason "stated intent: weld prep")
    9. `option` "Option scored": key `add_tool/G5/+Z/BC45-910480`, side shop, 2→1, changes_plan true
    10. `option` "Option scored": key `route_machine/5`, side shop, 2→1, changes_plan true
    11. `tool_call` "Model answered" (stage decide); +7.4 s
    12. `decision` "Decision made": I1, propose, `add_tool/G5/+Z/BC45-910480`, reason "Keeps the weld prep and drops a setup.", question null
    13. four `verify`/`error` "Sentence checked" events (stage check), index 0–3 with `text`; index 2 has ok false, reason "number outside a slot"
    14. `tool_call` "Model answered" (stage verify); +3.9 s
    15. three verify events (stage verify), index 0–2:
        - index 0: `verify` "Sentence verified"
        - index 1: `error` "Sentence not supported", reason "says more than its cites"
        - index 2: `verify` "Sentence verified"
    16. `verify` "Comment assembled" (stage publish)
    17. `verify` "Agent finished" (stage publish), `calls` = 3 calls:
        - `{stage:"plan", model:"meta-models/muse-spark-open-1026", host:"model-x.api.baseten.co", prompt_tokens:1166, completion_tokens:842, prompt_tokens_details:{cached_tokens:1152}, latency_s:6.1}`
        - the same for decide (611 out, 7.4 s)
        - `{stage:"verify", model:"muse-spark-1.3", host:"api.meta.ai", …, latency_s:3.9}`

        `fallbacks` `[]`.
    18. `comment_posted` "Posted the review"
    19. `run_finished` "Review finished"
  - **`part_a_fallback`:** the same opening, then:
    - plan has `error` "Model answer rejected" (attempt 1), then `tool_call` "Model answered" (attempt 2);
    - decide has `error` "Model answer rejected" ×2, then `error` "Stage fell back to the fixed script";
    - verify has `error` "No verifier: model prose is not published";
    - `calls` has no `latency_s`, and `fallbacks: ["decide"]`.

    It also contains a **second, older run** `v6bbbbbb-1` that ends with `run_finished` "Superseded by a newer version; nothing posted".

- [ ] **Step 1: Write the failing tests** in `tests/panel/test_router.py`, using a `client` fixture from `conftest.py`: `TestClient(FastAPI().include_router(make_router(list_reader(events), recording_writer, panel_secret="s3cret", allowed_docs=lambda: {"dSYN"})))`. `list_reader(events)` filters by document and `cursor > after`, and slices to `limit`.

  ```python
  def test_panel_page_needs_token_and_allowlisted_doc(client):
      assert client.get("/panel?documentId=dSYN&t=s3cret").status_code == 200
      assert "text/html" in client.get("/panel?documentId=dSYN&t=s3cret").headers["content-type"]
      for url in ("/panel?documentId=dSYN&t=wrong", "/panel?documentId=dSYN", "/panel?documentId=dOTHER&t=s3cret",
                  "/api/runs?documentId=dSYN&t=wrong", "/api/runs?documentId=dOTHER&t=s3cret"):
          r = client.get(url); assert r.status_code == 404 and r.content == b""

  def test_assets_are_served_from_an_allowlist_without_token(client):
      assert client.get("/panel/assets/panel.js").status_code == 200
      assert client.get("/panel/assets/../router.py").status_code == 404
      assert client.get("/panel/assets/secret.txt").status_code == 404

  def test_runs_groups_by_run_and_returns_cursor(client_fallback):   # part_a_fallback fixture
      body = client_fallback.get("/api/runs?documentId=dSYN&t=s3cret").json()
      assert [r["run_id"] for r in body["runs"]] == ["v6bbbbbb-1", "v7aaaaaa-1"]
      assert body["cursor"] == max(e["cursor"] for e in FALLBACK["events"])

  def test_runs_pages_past_the_first_page_and_keeps_the_latest_five():
      events = [ev(cursor=i + 1, run_id=f"r{i // 100:02d}") for i in range(1200)]   # 12 runs of 100
      body = client_for(events).get("/api/runs?documentId=dSYN&t=s3cret").json()
      assert [r["run_id"] for r in body["runs"]] == ["r07", "r08", "r09", "r10", "r11"]
      assert body["cursor"] == 1200
  ```

- [ ] **Step 2: Run them to verify they fail.** Run: `uv run pytest tests/panel/test_router.py -q`. Expected: FAIL, `ModuleNotFoundError: anville.panel`.

- [ ] **Step 3: Implement `make_router`, `/panel`, `/panel/assets/{name}` and `/api/runs`** in `router.py`.
  - `/api/runs` pages `reader(doc, after, PAGE)` until a page comes back shorter than `PAGE` or `MAX_PAGES` is reached. It groups the events by `run_id` in order of first appearance, keeps the last `RUNS_KEPT`, and returns events oldest first.
  - Every 404 is `Response(status_code=404)`.
  - The reader is synchronous: call it via `await asyncio.to_thread(...)`.

- [ ] **Step 4: Run the tests to verify they pass.** Run: `uv run pytest tests/panel -q`. Expected: all pass.

- [ ] **Step 5: Commit** (with Rayyan's go): `feat(panel): router with token/allowlist auth, /panel, assets, /api/runs`

---

### Task 3: `/api/stream` (SSE)

**Files:**
- Modify: `anville/panel/router.py`
- Test: `tests/panel/test_stream.py`

**Interfaces:**
- Consumes: `make_router(..., poll_s, heartbeat_s, stream_s)` from Task 2.
- Produces: the SSE framing from Task 1 §3. `GET /api/stream?documentId=&after=&t=`.

- [ ] **Step 1: Write the failing tests.** Use a router with `poll_s=0.01, heartbeat_s=0.05, stream_s=0.3`. Parse frames by splitting on `\n\n`.

  ```python
  def test_stream_sends_events_after_cursor_with_ids(stream_client):
      frames = read_all(stream_client, "/api/stream?documentId=dSYN&after=3&t=s3cret")
      ids = [int(f["id"]) for f in frames if f.get("event") == "trace"]
      assert ids == [e["cursor"] for e in LIVE["events"] if e["cursor"] > 3]
      assert frames[0].get("retry") == "1000"

  def test_last_event_id_wins_when_greater_than_after(stream_client):   # EventSource reconnect resends the old URL
      frames = read_all(stream_client, "/api/stream?documentId=dSYN&after=0&t=s3cret", headers={"Last-Event-ID": "10"})
      assert min(int(f["id"]) for f in frames if f.get("event") == "trace") == 11

  def test_stream_heartbeats_and_closes(stream_client_empty):
      raw = stream_client_empty.get("/api/stream?documentId=dSYN&after=0&t=s3cret").text   # returns once stream_s elapses
      assert ": hb" in raw

  def test_stream_picks_up_events_appended_while_open(): ...   # reader whose list grows on its 3rd call; the new cursor arrives

  def test_stream_auth(stream_client):
      assert stream_client.get("/api/stream?documentId=dSYN&t=bad").status_code == 404
  ```

- [ ] **Step 2: Run them to verify they fail.** Run: `uv run pytest tests/panel/test_stream.py -q`. Expected: FAIL, 404 from `/api/stream`.

- [ ] **Step 3: Implement the stream** as an async generator in a `StreamingResponse` with `media_type="text/event-stream"` and the headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`.
  - Loop until `clock() - start >= stream_s`. Each pass calls `reader(doc, last, PAGE)` via `asyncio.to_thread`, emits the frames, advances `last`, sends `: hb` once `heartbeat_s` has passed without a frame, then `await asyncio.sleep(poll_s)`.
  - Read the integer `Last-Event-ID` header; treat a bad value as 0.

- [ ] **Step 4: Run the tests to verify they pass.** Run: `uv run pytest tests/panel -q`. Expected: all pass.

- [ ] **Step 5: Commit** (with Rayyan's go): `feat(panel): /api/stream SSE with cursor resume and heartbeats`

---

### Task 4: `reduce.js`, runs, stages, chips, status (and the node test bridge)

**Files:**
- Create: `anville/panel/static/reduce.js`, `tests/panel/reduce.test.mjs`, `tests/panel/test_js.py`

**Interfaces:**
- Consumes: the fixture events from Task 2.
- Produces, as ES module exports of `reduce.js`:
  ```js
  export const STAGES = ["export","geometry","facts","intent","plan","decide","check","verify","publish"];
  export function initialState() // -> {runs: {}, order: [], cursor: 0}
  export function reduce(state, event) // pure; returns a new state; ignores events with cursor <= state.cursor
  export function latestRun(state) // -> Run | null
  // Run = {id, versionId, status: "live"|"done"|"error"|"superseded", trigger, startedTs, endedTs,
  //        stages: {[name]: {status: "pending"|"active"|"done"|"fallback"|"skipped", retries: number}},
  //        current: string|null, agentFailed: boolean,
  //        calls: [{stage, model, host, hostLabel, promptTokens, completionTokens, cachedTokens, latencyS}],  // null fields stay null
  //        liveCalls: [{stage, attempt}], issues: {[id]: {kind, faceGroup}}, intents: {[group]: {status, reason}},
  //        options: {[key]: {key, side, before, after, changesPlan, cost}}, rejected: [{editKind, faceGroup, reason, faceIds}],
  //        decisions: [{issueId, action, optionKey, reason, question}], sentences: [], ledgerUsed: boolean, commentPosted: boolean}
  export function hostLabel(host) // "Baseten" if /baseten/, "Meta API" if /meta\.ai/, "Fireworks" if /fireworks/, else host ?? "—"
  ```
- **The stage of an event without `detail.stage`:** "Exporting STEP" → `export`; "Setup count computed from geometry" → `geometry`; anything else leaves the rail alone.
- **Moving into a new stage** marks the previous `current` stage `done`, unless it is `fallback`.
- **Status changes:**
  - "Model answer rejected" adds a retry to its stage.
  - "Stage fell back to the fixed script" sets that stage to `fallback`.
  - "No verifier…" sets `verify` to `skipped`.
- **"Agent finished"** replaces `liveCalls` with `calls`.
  - `cachedTokens` comes from `cached_tokens` or `prompt_tokens_details.cached_tokens`.
- **The run's status:**
  - `run_finished`: "Superseded…" → `superseded`; after an `error` "Review failed" → `error`; otherwise `done`.
  - On any `run_finished`, a still-`active` stage becomes `done`.
- **Other flags:**
  - "Agent failed; posting the setup count instead" sets `agentFailed`.
  - `ledgerUsed` is true once any event has a non-empty `detail.ledger_used`.

- [ ] **Step 1: Write the failing tests** in `reduce.test.mjs`, using `import {test} from "node:test"`. Load fixtures with `JSON.parse(readFileSync(new URL("./fixtures/…", import.meta.url)))` and fold them with `events.reduce(reduce, initialState())`.
  - `live run ends done, all agent stages done, 3 chips with host labels Baseten, Baseten, Meta API, latency 6.1/7.4/3.9, cached 1152`
  - `fallback run: plan retries 1 and done, decide fallback, verify skipped, chips latencyS null`
  - `superseded older run has status superseded and no active stage`
  - `mid-run state (first 8 events of the live run) is live with current "plan"`
  - `replaying an event with an old cursor changes nothing` (`deepStrictEqual`)
  - `agent failed: event "Agent failed; posting the setup count instead" then run_finished → agentFailed true, status done`
  - `hostLabel(null) === "—"`

- [ ] **Step 2: Write the bridge `tests/panel/test_js.py`.** It runs `node --test tests/panel/` with `subprocess.run(..., check=False)`, asserts return code 0, and prints the output on failure. It calls `pytest.skip("node not installed")` only when `shutil.which("node")` is None.

- [ ] **Step 3: Run the tests to verify they fail.** Run: `node --test tests/panel/`. Expected: FAIL, cannot find module `reduce.js`.

- [ ] **Step 4: Implement `reduce.js`** so the tests pass. Use plain objects and `structuredClone` for purity.

- [ ] **Step 5: Run the tests to verify they pass.** Run: `node --test tests/panel/ && uv run pytest tests/panel -q`. Expected: all pass.

- [ ] **Step 6: Commit** (with Rayyan's go): `feat(panel): pure trace reducer for runs, stages and model-call chips`

---

### Task 5: `reduce.js`, options, decisions → cards, sentences, the ring

**Files:**
- Modify: `anville/panel/static/reduce.js`
- Test: `tests/panel/reduce.test.mjs`

**Interfaces:**
- Produces:
  ```js
  export function optionLabel(key) // code-rendered words from the option key:
  //  "add_tool/<G>/<dir>/BC<angle>-<name>" -> "Buy <name> back-chamfer cutter (<angle>°)"
  //  "add_tool/<G>/<dir>/<name>"           -> "Add tool <name>"
  //  "route_machine/<n>"                   -> "Route to a <n>-axis machine"
  //  "remove_feature/<G>"                  -> "Remove feature <G>"
  //  "set_min_radius/<G>/r<r>"             -> "Min radius <r> mm on <G>"
  //  "set_depth/<G>/<dir>/<d>"             -> "Depth <d> mm on <G>"
  //  anything else                         -> the key itself
  export function decisionCards(run) // -> [{issueId, before, after, design: {text, note}, shop: {text, note}, question, cost}]
  export function sentenceSummary(run) // -> {verified: n, total: n, items: [{index, text|null, status, reason}]}
  //   status: "verified" | "rejected-check" | "rejected-verify" | "unverified"
  export function ringState(run) // -> {saved, reviewing, reviewed, setupSheet, learned, innerArc}
  //   each value is "off" | "lit" | "active" | "next"
  ```
- **Cards** are built from `propose` decisions only, in event order. A decision whose `option_key` has no matching option is skipped. Any `action` other than `propose`/`quiet` is ignored.
  - **`design`:**
    - `{text: "<group> kept", note: "stated intent: <reason>"}` if the issue's group intent is `protected`;
    - `{text: optionLabel(key), note: null}` if the option's side is `part`;
    - else `{text: "No change", note: null}`.
  - **`shop`:**
    - `{text: optionLabel(key), note: payback_mid ? "pays back from part <payback_mid>" : "pays back in — parts"}` if the side is `shop`;
    - else `{text: "No change", note: null}`.
  - **`cost`:** the option's `cost`, or null.
- **Sentences:**
  - A check event at `index` i sets item i to `rejected-check` (if not ok) or `unverified` (if ok).
  - A verify event at index j applies to **the j-th check-passed item in index order**, setting it to `verified` or `rejected-verify`.
  - `text` comes from either event, whichever has it.
  - `total` counts the check events.
- **Ring:**
  - `saved`: `lit` once `run_started` arrives.
  - `reviewing`: `active` while status is `live`, `lit` after.
  - `reviewed`: `lit` on `comment_posted`.
  - `setupSheet`: `next` once reviewed.
  - `learned`: `lit` iff `ledgerUsed`.
  - `innerArc`: `lit` iff `trigger` is `reply` or `ask`.

- [ ] **Step 1: Write the failing tests:**
  - `optionLabel` for each of the 6 key forms plus an unknown key;
  - the live run's cards are exactly one, `{before: 2, after: 1, design: {text: "G5 kept", note: "stated intent: weld prep"}, shop: {text: "Buy 910480 back-chamfer cutter (45°)", note: "pays back in — parts"}, question: null, cost: null}`;
  - a `question` on the decision passes through to the card;
  - a decision with `action: "ask"` produces no card and doesn't throw;
  - the live run's sentences:
    - `{verified: 2, total: 4}`;
    - statuses by original index `["verified","rejected-verify","rejected-check","verified"]`. *(Review Focus 3: verify index 1 maps to original index 1, and verify index 2 to original index 3.)*
  - fallback run sentences: none rejected-verify, all check-passed are `unverified`;
  - `ringState`:
    - for the live run: `{saved:"lit", reviewing:"lit", reviewed:"lit", setupSheet:"next", learned:"off", innerArc:"off"}`;
    - mid-run: `reviewing: "active"`, `reviewed: "off"`;
    - with `ledger_used` added to one event: `learned: "lit"`.

- [ ] **Step 2: Run them to verify they fail.** Run: `node --test tests/panel/`. Expected: FAIL on the missing exports.

- [ ] **Step 3: Implement `optionLabel`, `decisionCards`, `sentenceSummary` and `ringState`**, and extend `reduce` to collect `issues`, `intents`, `options`, `rejected`, `decisions` and `sentences`.

- [ ] **Step 4: Run the tests to verify they pass.** Run: `node --test tests/panel/`. Expected: all pass.

- [ ] **Step 5: Commit** (with Rayyan's go): `feat(panel): decision cards, sentence verdicts and ring state from the trace`

---

### Task 6: `pace.js`, burst replay at the recorded timing

**Files:**
- Create: `anville/panel/static/pace.js`, `tests/panel/pace.test.mjs`

**Interfaces:**
- Produces:
  ```js
  export const MIN_MS = 120, MAX_MS = 1200, BURST_MS = 250;
  export function paceDelay(prev, cur) // ms to wait before rendering `cur`; prev/cur = {ts (s), arrivedMs}
  //   0 if prev is null or cur.arrivedMs - prev.arrivedMs >= BURST_MS, else clamp((cur.ts - prev.ts) * 1000, MIN_MS, MAX_MS)
  export function createQueue(apply, {setTimer = setTimeout, now = Date.now} = {}) // -> {push(event), replay(events)}
  //   push stamps arrivedMs = now(); drains in order, waiting paceDelay between items; replay(events) stamps all with one arrivedMs
  ```

- [ ] **Step 1: Write the failing tests:**
  - `paceDelay`:
    - `null` prev → 0;
    - a 5 s ts gap within a burst → 1200;
    - a 0.05 s gap within a burst → 120;
    - a 0.4 s gap within a burst → 400;
    - arrivals 300 ms apart → 0.
  - `createQueue` with a fake timer:
    - `replay` of the live run applies events in cursor order;
    - the total scheduled delay equals the sum of the clamped gaps.

- [ ] **Step 2: Run them to verify they fail.** Run: `node --test tests/panel/pace.test.mjs`. Expected: FAIL.

- [ ] **Step 3: Implement** `pace.js`.

- [ ] **Step 4: Run the tests to verify they pass.** Run: `node --test tests/panel/`. Expected: all pass.

- [ ] **Step 5: Commit** (with Rayyan's go): `feat(panel): replay trace bursts at their recorded timing`

---

### Task 7: The page: `view.js`, `panel.js`, HTML/CSS, and the dev server

**Files:**
- Create: `anville/panel/static/view.js`, `anville/panel/static/panel.js`, `anville/panel/static/panel.css`, `anville/panel/dev.py`, `tests/panel/view.test.mjs`
- Modify: `anville/panel/static/panel.html` (the real shell)

**Interfaces:**
- Consumes: `reduce.js` (all exports), `pace.js` (`createQueue`).
- Produces:
  ```js
  // view.js
  export function esc(s) // HTML-escape & < > " '
  export function renderHTML(state) // -> HTML string for the latest run (pure; tested); view applies it to #app
  ```
- `dev.py` runs with `uv run --with uvicorn python -m anville.panel.dev [fixture.json] [--speed 1.0]`. It serves `make_router` on `127.0.0.1:8765`, with token `dev`. Its reader *drips* the fixture: an event becomes visible once `(ts - first_ts) / speed` seconds have passed since startup. It prints `http://127.0.0.1:8765/panel?documentId=dSYN&t=dev`.
- **Layout** (DESIGN §4.1–4.6):
  - **Header:** "Anville · version <first 6 chars of the version_id>" and the status ("● live · Ns", "done · Ns", "superseded", "ended with an error"). The run timer is `ts` difference.
  - **Ring SVG:**
    - the wall is a dashed vertical line;
    - nodes: `saved` on the west, `reviewed` on the north on the wall, `setupSheet` on the east, `learned` on the south on the wall;
    - inner arc between west and north;
    - the `active` arc has an animated dash offset;
    - `next` pulses;
    - labels: "Saved", "Reviewed", "Setup sheet", "Shop note → learned", "reply ⇄ re-plan".
  - **Decision card(s):** they appear only once `decisionCards(run).length > 0`, and slide in with a 300 ms CSS transition.
    - The headline reads "`before` → `after` setups".
    - Then the Design side | Shop side grid.
    - Then a question line if set.
    - Then cost: "one-time $lo–hi" if `cost`, else `$ —`.
    - Then a disabled button "Open setup sheet".
  - **Agent section:**
    - The stage rail as pills: done green, active blue with a pulse, fallback amber with "↺n" for retries, skipped grey.
    - Chips: stage · model short name · host badge · "in/out tok · cached · s". Missing values show "—".
    - The option list, with rejected edits struck through and their reason.
    - "N of M sentences verified", with rejected sentences struck through and verified ones with a green bar. Items without text read "Sentence n+1".
  - **When agent failed:** a muted line, "Agent failed; posted the setup count instead".
- `panel.js` reads `documentId`, `t` and `after` from `location.search`.
  - It fetches `/api/runs?documentId=…&t=…` (absolute paths, since the page is served at `/panel` and its assets at `/panel/assets/`) and folds the result. The latest run, if `done`, renders immediately; if it's still `live`, it goes through `queue.replay`.
  - It then opens `EventSource("/api/stream?documentId=…&after=<cursor>&t=…")`, and each `trace` event goes to `queue.push`.
  - Earlier runs are listed under "Earlier runs", and clicking one calls `queue.replay` on a fresh state.

- [ ] **Step 1: Write the failing tests** in `view.test.mjs`:
  - `esc` covers the 5 characters;
  - `renderHTML` of the live run contains "2", "→", "1", "G5 kept", "Buy 910480 back-chamfer cutter (45°)", "Meta API" and "2 of 4 sentences verified";
  - **Review Focus 1:** with a decision `reason` of `<img src=x onerror=alert(1)>`, the output contains `&lt;img` and not `<img src=x`;
  - mid-run `renderHTML` contains "● live" and no decision card;
  - a chip without `latency_s` shows "—".

- [ ] **Step 2: Run them to verify they fail.** Run: `node --test tests/panel/view.test.mjs`. Expected: FAIL.

- [ ] **Step 3: Implement** `view.js`, `panel.js`, `panel.css`, `panel.html` and `dev.py`.

- [ ] **Step 4: Run all the tests.** Run: `node --test tests/panel/ && uv run pytest -q`. Expected: all pass (the 277 existing tests plus the panel's).

- [ ] **Step 5: Verify in a browser.**
  - Run `uv run --with uvicorn python -m anville.panel.dev tests/panel/fixtures/part_a_live.synthetic.json`.
  - Open the printed URL at 440 px and at 320 px.
  - **The run passes if:**
    - the ring's arc animates while live;
    - the stage pills advance in order;
    - the decision card slides in after decide;
    - the chips fill in at "Agent finished";
    - the status ends "done".
  - Take screenshots at both widths for Rayyan.

- [ ] **Step 6: Commit** (with Rayyan's go): `feat(panel): the live ring, decision card and agent view, with a fixture dev server`

---

### Task 8: `/api/ask` (contract endpoint; its UI is P1)

**Files:**
- Modify: `anville/panel/router.py`
- Test: `tests/panel/test_ask.py`

**Interfaces:**
- Consumes: `Writer` from Task 2.
- Produces: `POST /api/ask?t=`, with a JSON body `{request_id, documentId, workspaceId, elementId, microversion_id?, face_ids, question}`.
  - It calls `writer("ask:" + request_id, documentId, payload)`, where `payload = {"event": "panel.ask", **body, "microversion_id": body.get("microversion_id"), "received_at": clock()}`.
  - It returns `{"event_id": "ask:<request_id>"}`: 202 if inserted, 200 if a duplicate.
  - **Validation (422):**
    - `request_id` must be a UUID;
    - `face_ids` is a list of at most 20 strings, each at most 64 characters;
    - `question` is a string of at most 500 characters (it may be empty);
    - the ids are non-empty strings.
  - A non-allowlisted document or a bad `t` → 404.

- [ ] **Step 1: Write the failing tests:**
  - first POST → 202, and the writer received the key `ask:<uuid>` and `payload["event"] == "panel.ask"`;
  - the same `request_id` again (the writer returns False) → 200 with the same `event_id`;
  - a non-UUID `request_id` → 422;
  - 21 face ids → 422;
  - `documentId: "dOTHER"` → 404;
  - a bad `t` → 404.

- [ ] **Step 2: Run them to verify they fail.** Run: `uv run pytest tests/panel/test_ask.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** with a pydantic model, `uuid.UUID(...)` validation, and `asyncio.to_thread(writer, ...)`.

- [ ] **Step 4: Run the tests to verify they pass.** Run: `uv run pytest tests/panel -q`. Expected: all pass.

- [ ] **Step 5: Commit** (with Rayyan's go): `feat(panel): POST /api/ask inserts an idempotent panel.ask event`

- [ ] **Step 6: Hand off to main** (SendMessage `anville-81`) the exact mount line:
  ```python
  api.include_router(make_router(
      lambda d, a, n: StoreSvc().call.remote("trace_after", d, a, n),
      lambda key, doc, payload: StoreSvc().call.remote("record", key, doc, payload),
      panel_secret=os.environ["ANVILLE_PANEL_TOKEN"], allowed_docs=_allowed))
  ```
  Also send the Amendment 1 field names for asks #2–#4. Then open the PR (Greptile reviews, Rayyan merges).
