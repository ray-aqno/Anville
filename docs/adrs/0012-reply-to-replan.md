# ADR 0012: A designer's reply becomes a constraint, and Anville re-plans in the same thread

- **Status:** Proposed
- **Date:** 2026-10-30
- **Depends on:** ADR 0003 (webhooks keyed by `commentId`, F1 self-events), ADR 0006 (event store, per-document claims), ADR 0009 and its Amendment 1 (stage graph, flag then suggest; `tag_intents` already reads ledger constraints)
- **Numbering:** 0010 is the shop lane's cost-ranked fixes; 0011 is Fireworks.

## Context

- **The gap.** The critical path in the brief is: webhook round trip → setup counts → review in Onshape → **reply and re-plan** → second-part learning. Today the job handles `onshape.comment.create` only as far as a log line (`modal_app.py`, "designer_comment"). Nothing turns a reply into a second turn.
- **What exists.**
  - `run_review(..., constraints=...)` already re-plans from ledger constraints: `tag_intents` makes the newest `keep` constraint *protected* and `may_change` *free*. The eval gate's two-turn scenarios ("keep", "not needed") exercise exactly this, in-process.
  - The `constraint` ledger exists in the store (`LEDGERS`). Each STEP is kept on the data volume by version ID, so a re-plan re-runs geometry without a second export.
  - The posted review's comment ID is recorded (`complete_post(event_key, "review", comment_id)`).
- **Onshape comments, from the live OpenAPI spec (`cad.onshape.com/api/openapi`, fetched 2026-10-30):**
  - **Verified (spec):** `GET /comments/{cid}` returns `BTCommentInfo`, which has `message`, `parentId`, `topLevel`, `replyCount`, `documentId`, `versionId` and `user`. `POST /comments` takes `BTCommentParams`, which includes `parentId`.
  - **Inference:** that `parentId` is the thread root a reply sits under, and that posting with `parentId` makes a threaded reply. The spec gives the fields no descriptions. One live call settles it (see Open questions).

## Decision

1. **Which replies count.** A `comment.create` is a reply to Anville when the fetched comment's `parentId` is a comment Anville posted (a `review` or `replan` post in the store). Anything else stays quiet: a top-level comment, or a reply in someone else's thread. Anville's own comments are already dropped by F1.
2. **Reading the reply is the model's job.** Muse Spark Open makes one tool call, `read_reply`, given the issues the parent comment raised (face groups, proposals, questions) and the reply text. It returns zero or more `{face_group, rule: keep | may_change, quote}`. Code validates every item:
   - `face_group` must be one the parent comment raised;
   - `quote` must be a verbatim substring of the reply. This is the grounding check, so a rule can't be invented.
   - An invalid answer retries once, as every stage does. No regex or keyword reading of free text, anywhere.
3. **The rule goes in the ledger with provenance.** `append_ledger("constraint", scope="doc:<documentId>", payload={face_group, rule, reason: quote, version_id}, provenance={comment: <reply id>, review: <parent event key>})`.
4. **Re-plan the same version.** Re-run geometry on that version's stored STEP, then `run_review(..., constraints=<all constraints for the document>)`. A newer version is the next save's job: its own review reads the same ledger.
5. **Answer in the thread.** Post the re-plan with `parentId` = the thread root, through the same `post_as_anville` path, reserved and completed as kind `replan` (idempotent per reply).
6. **When nothing is read.** If `read_reply` returns no rules, or the model is unavailable, Anville posts one code-rendered line naming the issues still open, for example: "Anville read this reply, but not as a decision on G3; the plan stands at 2 setup(s)." It never posts a bare question, and never infers a rule in code.
7. **The trace.** The panel sees a run with stages `read_reply` → `plan` → `decide` → `verify` → `publish`, plus a "Constraint recorded" event carrying the quote.

## Options considered

- **Keyword parsing ("keep", "not needed") in code.** It would be fast and free, but it breaks "Meta models do all AI work at runtime", and it misreads real replies ("we can't lose it, it clears the weld"). Rejected.
- **Re-run the whole job from a fresh STEP export.** Simpler code, but a second translation per reply, and translation is the slowest step. Rejected in favour of the stored STEP.
- **Re-plan the newest version instead of the one replied on.** Closer to "latest version reviewed", but the reply answers questions about one version's face groups (`G3`), and labels aren't stable across versions until face anchoring (RFC 0003) lands. Rejected for now; see Consequences.
- **Answer as a new top-level comment.** Needs no `parentId` assumption, but it breaks "the comment thread is the only place conversation happens". Kept only as the fallback if the live check shows `parentId` doesn't thread.

## Trade-offs

- **One more model call per reply.** It costs about 2–5 s, plus the review's three calls. The target stays under a minute from reply to answer, and geometry on a stored STEP takes a few seconds.
- **Constraints are scoped to a document, keyed by face-group label.** That's correct within one version. Across versions a label can point at a different face. Each entry carries `version_id`, so a later reader can tell.

## Consequences

- **Easier:** the demo's middle beat (save → review → reply → re-plan) runs live in Onshape. Every turn is in the trace and in the ledger, with the designer's own words as the reason.
- **Harder:** cross-version and cross-part memory of *designer* constraints. "This bracket's weld chamfer stays" on Part B needs feature identity beyond labels: S5's intent from feature names, or RFC 0003's anchoring. The second-part learning beat therefore stays on the shop ledger (`tool_acquired`), which needs no face identity.
- **To revisit:** when RFC 0003 is decided, re-key constraints by anchored face; and decide whether a reply on an old version should re-plan the newest one.

## Open questions

1. **`parentId` semantics (one Onshape call, needs Rayyan's go).** `GET /comments/{cid}` on a reply Rayyan makes in the sandbox document, to confirm that `parentId` is the root and `topLevel` is false. Then one threaded test post.
2. **The webhook subscription.** Does the current registration include `onshape.comment.create`? (`events.py` routes it, so it's likely; check the registration call before the demo.)
3. **Reply eval.** The models lane adds recorded replies to the gate: "keep", "not needed", ambiguous, adversarial, and off-topic. ADR 0004 already lists "reply-parse accuracy on recorded replies".
