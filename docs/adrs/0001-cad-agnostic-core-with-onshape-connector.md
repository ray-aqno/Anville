# ADR 0001: CAD-agnostic core with an Onshape connector

- **Status:** Proposed
- **Date:** 2026-10-26

## Context

Anville reviews CAD part versions for manufacturability and closes the loop with the shop. Onshape is the interface: webhooks trigger reviews, and the output appears as Onshape comments. But Onshape's REST API is metered. Free, Standard, and EDU Student plans get 2,500 calls per user per year; Professional gets 5,000. Incoming webhooks and 4xx/5xx responses do not count ([Onshape API limits](https://onshape-public.github.io/docs/auth/limits/), checked 2026-10-26). If every review, development iteration, and test ran through Onshape, the budget would be gone within days.

Separately, the core value (geometry facts, setup planning, cost, learned constraints) has nothing to do with Onshape. It needs a STEP file and a shop profile.

## Decision

Split Anville into a **core** and a **connector**.

- **Core.** It never calls a CAD API. Input: STEP plus a shop profile. Output: a review with structured findings and fix intents.
- **Two interfaces,** each with exactly two implementations:
  - `CadSource`: `OnshapeSource` (webhook plus STEP export) and `LocalSource` (a folder of `.step` files).
  - `ReviewSink`: `OnshapeSink` (comments and a branch) and `LocalSink` (JSON or a fallback page).
- **Fixes are CAD-neutral intent** (for example: "raise the min concave radius on face group G to 6 mm"). The connector compiles intent into a CAD edit; for Onshape, Muse Code produces a Part Studio feature on a new workspace.
- **Every Onshape call goes through one budgeted client.** It counts calls, caches STEP files by `versionId`, and refuses to send past a daily cap.
- **Recorded fixtures.** Real webhook payloads and API responses are recorded once and replayed in tests.

## Options considered

1. **Onshape-native throughout.** Simplest wiring, but every test spends API budget, and the core can't run without Onshape.
2. **Core plus connector (chosen).**
3. **Generic plugin system for many CAD tools.** Speculative. Only one connector is needed now.

## Trade-offs

- More interfaces than an Onshape-only build, which means a little more code up front.
- Fix-as-intent adds a compile step. In exchange, a failed CAD edit can fall back to the same intent shown as a written fix plus a render.

## Consequences

- **Easier:** development and tests at zero API cost; another CAD connector later; datasets and lessons that aren't tied to Onshape's schema.
- **Harder:** the demo must still run end to end through Onshape. Local mode is for development and fallback only.
- **Revisit** if the per-review call count exceeds about 5, or if a second connector is actually needed.
