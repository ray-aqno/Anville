# ADR 0005: Multimodal evidence (drawings and shop photos), never multimodal geometry

- **Status:** Proposed
- **Date:** 2026-10-26
- **Depends on:** ADR 0002, ADR 0003, ADR 0004

## Context

STEP gives Anville the shape of a part. It does not carry two things the co-optimizer needs to choose well:

- **Design intent.** Tolerances, surface finish, GD&T, and "do not modify" notes live on the drawing.
- **The shop's reality.** The tools actually on the wall, the fixtures, and how setups are really done live in the shop.

Without those, Anville has to ask questions it could have answered itself, and the shop profile has to be typed in by hand.

Muse Spark accepts image and PDF input and supports JSON-schema-constrained output on Meta Model API (dev.meta.ai docs and the official `meta-model-cookbook`, checked 2026-10-26). Locked decision 3 still holds: setup counts and every geometric number are computed, never read off a picture.

## Decision

Add an **Evidence** layer to the world model:

```
world = PartFacts (computed from STEP) + ShopProfile + Evidence
```

1. **Drawing PDF → design intent.**
   - Muse Spark reads the part's drawing and returns callouts (tolerance, finish, GD&T, note) as schema-constrained JSON. Each callout carries its page and region.
   - Every callout must be **mapped to a face group in PartFacts**, by feature type and nominal dimension against the computed value. An unmapped callout is shown to the designer as unmapped, never silently applied.
   - Mapped callouts become **constraints** on `PartEdit` candidates. For example, a toleranced radius blocks `set_min_radius` unless the designer overrides it. So a drawing can change which branch wins.
2. **Shop photos → proposed profile entries.**
   - Photos of the tool wall, fixtures, or setup sheets go to Muse Spark. It returns proposed `ShopProfile` entries (tool type, diameter, reach or length, fixture type) as schema-constrained JSON.
   - Entries stay **`pending`** until the shop lead confirms them in a reply. Only confirmed entries take part in planning.
   - Each confirmed entry records the photo and model that proposed it.
3. **The same rule as ADR 0003: the model proposes, code or a person decides.** Evidence never edits a number directly. It becomes a proposed fact with a source, a model ID, and a confidence. It is checked against computed geometry or confirmed by a human before the planner uses it.
4. **SAM 3.1 render markup stays as planned** (ADR 0004). The model checking its own markup is deferred.

## Options considered

1. **Text plus SAM only.** Least work, but the planner stays unaware of intent, and shop onboarding is manual.
2. **Evidence layer with confirmation (chosen).**
3. **Read geometry from images too.** Rejected: it violates "compute, don't guess" and the results can't be verified.

## Trade-offs

- Each demo part needs a drawing with a few real callouts.
- The shop-photo path needs real photos from the shop. Without them it degrades to a typed profile, which is labeled as such.
- Matching callouts to faces is new logic. Ambiguous cases go back to the designer as questions.

## Consequences

- **Easier:** fewer intent questions to the designer, onboarding a shop from photos, and multimodal reasoning that visibly changes a decision. That keeps Anville clear of the "simple image analyzer" anti-pattern, because images are inputs to a loop that acts and verifies.
- **Harder:** evidence extraction needs its own eval fixtures (a known drawing and known photos with expected outputs) under the ADR 0004 selection rule.
- **Revisit:** if callout-to-face matching proves unreliable on the demo parts, fall back to the designer confirming each callout in a reply.
