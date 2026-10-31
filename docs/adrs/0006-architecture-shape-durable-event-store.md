# ADR 0006: Architecture shape: per-event jobs over one durable event store

- **Status:** Accepted (Rayyan Aquino, 2026-10-30, by delegated review: still within the system's shape)
- **Notes:** §9 (evidence intake) falls with ADR 0005, rejected. §10 (anchoring) waits on RFC 0003.
- **Date:** 2026-10-27
- **Depends on:** ADR 0001, ADR 0003, ADR 0005

## Context

The Day-1 spike is deployed: an Onshape webhook reaches a Modal ASGI endpoint, which spawns a job that posts a comment and exports STEP. It worked end to end on a sandbox part (verified 2026-10-26: about 12 s from save to comment, about 17 s from save to STEP file stored).

Reviewing the spike turned up three defects (verified by reading `anville/modal_app.py`):

1. **At-most-once delivery.** The endpoint marks an event `seen` *before* spawning the job. If the spawn or the job fails, the event is lost and a redelivery is dropped as a duplicate.
2. **Non-atomic de-duplication.** It checks `seen` and then sets it as two separate operations on a `modal.Dict`, so two concurrent deliveries can both pass.
3. **Jobs can't be resumed.** A container that dies mid-job leaves no record of how far it got.

Onshape webhooks are unsigned and not de-duplicated. Onshape does not document a retry policy. Its webhook docs say delivery continues "until the app returns an error or fails to respond for an extended period", and the webhook record has a `droppedEventCount` field. So returning an error is more likely to drop events or disable the hook than to trigger a retry (inference).

Also verified 2026-10-27: an API-created comment can anchor to a single face. `elementQuery: 'query=qTransient("<faceId>");'` on a saved version highlights that face in Onshape. Transient face IDs are valid for one microversion only. `POST /partstudios/.../idtranslations` maps them between versions.

The options were deliberated: per-event stateless jobs plus a shared store (the spike), a durable per-document state machine, and two-agent negotiation (a designer-side agent and a shop-side agent with the planner as referee).

## Decision

1. **Shape: stateless per-event jobs over one durable event store.** Not a state-machine framework, and no new service.
2. **The store is SQLite on a Modal Volume**, with WAL, and only one writer: a single Modal function limited to `max_containers=1`.
   - `events` table: the primary key is the event's idempotency key (`version:<vid>`, `comment:<cid>`, `tx:<tid>`). Rows are written with `INSERT OR IGNORE`. Columns include `status` (`received → claimed → done | failed`), `claimed_at`, and the attempt count.
   - `doc_state`: the latest version under review for each document.
   - Ledgers (constraints, shop outcomes) are append-only and written only after the planner has verified a result.
3. **Delivery order:** insert the event row → return 200 → spawn the job. Return non-200 only if the insert itself fails.
4. **Recovery:** a scheduled reaper re-spawns rows left `received`, or `claimed` past a timeout.
5. **Version guard:** a job compares its version with `doc_state` before posting. A result for a version that has been superseded is discarded, not posted.
6. **Idempotent side effects:** each comment is recorded against the event that produced it, and Anville checks that record before posting. IDs of everything Anville posts are registered so their webhooks can be dropped (ADR 0003, F1).
7. **`modal.Dict` holds only ephemeral data:** self-created IDs and translation-done flags. It is the fallback store if SQLite on the Volume fails the concurrency test. It would keep the same single writer and the same tables.
8. **Agent boundary stays as ADR 0003 has it:** one bounded orchestrator with a call cap, timeouts, and the fixed-script fallback. The agent trace is a streamed log.
9. **Evidence intake is hybrid (ADR 0005).** Drawings are extracted when uploaded and cached by content hash. Shop photos are read on demand through a `read_evidence` tool, and entries stay pending until the shop lead confirms them.
10. **Comment anchoring:** flags anchor to faces with `qTransient` IDs from the version under review. A re-plan on a newer version re-anchors to that version, using `idtranslations` when the face has to be carried over.
11. **Not doing:** two-agent negotiation (deferred), and self-hosting Muse Glimmer (dropped).

## Options considered

1. **Spike as-is (stateless jobs + `modal.Dict`).** Least code, but loses events and double-posts under concurrency.
2. **Per-event jobs + durable SQLite event store (chosen).**
3. **Explicit per-document state machine / event sourcing.** Strongest guarantees, but more machinery than one service and a few documents need.
4. **Two-agent negotiation with the planner as referee.** Good to watch, but adds a second loop that has to converge, and the planner is still the one that decides.

## Trade-offs

- With a single writer, every write goes through one container. That's fine at a few events a minute, but it's a ceiling.
- SQLite on a network Volume needs explicit commits and reloads between containers. The concurrency test below is what decides whether it holds.
- The reaper adds a scheduled function and a timeout to tune.

## Consequences

- **Easier:** redeliveries, crashes, and two documents edited at once become test cases rather than incidents. The ledgers become the data a cross-part learning step can read.
- **Harder:** every new event type needs an idempotency key and a status flow.
- **Acceptance test:** 20 concurrent duplicate deliveries, plus two documents edited at once, plus a container killed mid-job. To pass: one claim and one comment per event, no lost events, the reaper recovers the killed job, stale results are discarded, and p95 latency is recorded.
- **Revisit:** if the acceptance test fails on the Volume (switch to the `modal.Dict` fallback), or if event volume outgrows a single writer.
