# Anville

**Your home for manufacturing infrastructure.**

*Anvil + -ville, the way Nashville is a town: the place where everything that makes parts comes to live.*

Anville closes the design loop between engineers and the shop floor, and every loop makes the next one shorter.

*Built during Meta's 2026 Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). Every commit in this repo is from the event.*

## The edge: a workflow, not a gap fill

Manufacturing software stops at company walls. Design tools help a team design; shop software helps a shop quote and schedule. **The work that crosses between them** — a part version, "we can't make this", a fix, a tooling change, what actually happened at the spindle — runs on email and PDFs, and nobody owns it.

Anville is that workflow, for **hardware teams that design parts and send them to their shops**:

```
version saved in CAD → review → negotiation in the comment thread → fix branch or shop-side change
                     → setup sheet → outcome → the next part arrives already informed
```

What makes it more than a checker:

- **It computes before it speaks.** Setup counts, tool reach and fixture clearance come from the part's geometry (STEP), never from a language model's guess. A review comment's numbers are slots filled by code; a number the model writes itself is rejected.
- **The factory is part of the design space.** A fix can be a change to the part *or* to the shop. When a feature's purpose is stated (say, a chamfer that clears a weld bead), Anville won't edit it; it looks for a shop-side fix instead — a cutter to buy, run in relieved soft jaws — and checks that the cut is physically possible, jaws included.
- **It works in CAD, on a branch.** With `ANVILLE_FIX_BRANCH=on`, after the review is posted, the part edit the designer is expected to decline (removing the flagged feature, when that lowers setups) is built as a real feature edit on an `anville/alt-<run>` branch, never on the designer's main, and its setups are measured from that branch's own export. Muse Wonder writes the patch; code's patch is used if Muse Wonder is unavailable or its patch is rejected, and a branch that fails to build is reported as a prediction, not a measurement ([ADR 0002 Amendment 2](docs/adrs/0002-co-optimizer-edit-the-world.md), [ADR 0004 Amendment 1](docs/adrs/0004-model-roles-and-selection.md)). The flagged face is shown on a render painted by code from the B-rep, with SAM 3.1's outline drawn only when SAM runs and its mask agrees with code's pixels ([ADR 0007 Amendment 4](docs/adrs/0007-anville-inside-onshape-identity-and-panel.md)). Pinning the comment itself to the face is deferred past the hackathon ([RFC 0003](docs/rfcs/0003-face-anchoring.md)).
- **It learns in records, not weights.** Constraints, shop capabilities and outcomes are stored with provenance; the designer's constraints and the tools the shop has bought are fed back at the next review, so they carry over to a new base model. A designer's reply becomes a constraint (on main); a shop's note, sent from the setup sheet's traveler page, becomes an outcome. Both are on main and deployed, and ran live end to end on 2026-10-31.

## Status

| Part | State |
|---|---|
| Onshape webhook → durable event store → per-event jobs on Modal | working ([ADR 0006](docs/adrs/0006-architecture-shape-durable-event-store.md)); acceptance tests passed on staging |
| Geometry: STEP → face facts → minimum setups | working ([ADR 0008](docs/adrs/0008-geometry-stack-and-setup-count.md)); checked against Onshape's own face count on a real export |
| Edit the world and re-plan (`evaluate_edits`, `remove_feature`) | working ([ADR 0002](docs/adrs/0002-co-optimizer-edit-the-world.md)) |
| Back-chamfer cutter in relieved soft jaws: six checks incl. swept head/neck solids | working ([RFC 0002](docs/rfcs/0002-shop-profile.md), closed: implemented in `anville/shop`) |
| Agent review: plan and decide-and-write on Muse Spark Open, each sentence and question the model writes verified by `muse-spark-1.3` (code renders the rest), inside a gated action space | working ([ADR 0009](docs/adrs/0009-agent-loop-stage-graph.md)); an eval gate on the live models, scored in code (PR #31) |
| Shop-side fixes, priced and ranked: buying a cutter, with its payback; costs checked at every corner of the shop's input ranges | working ([ADR 0010](docs/adrs/0010-cost-ranked-any-intent-fixes.md)) |
| The designer's reply in the thread → a constraint → a re-plan | working ([ADR 0012](docs/adrs/0012-reply-to-replan.md)); ran live on 2026-10-31 |
| Setup sheet stored with each version's review and linked from its comment when the panel is mounted (a re-plan doesn't make a new sheet); the shop's traveler page and its notes → the shop ledger; the next part planned with what the shop owns | working (PRs #40, #42, #44); ran live on 2026-10-31 |
| Docked Onshape panel: live loop ring, decision card, model chips | on main ([ADR 0007](docs/adrs/0007-anville-inside-onshape-identity-and-panel.md)); mounted once its token is set (and differs from the webhook and OAuth admin secrets) |
| Fix branch: after the review, the declined removal built on an `anville/alt-<run>` branch (Muse Wonder's patch, or code's) and measured from the branch's export, answered in the thread | on main (PRs #47, #48, #51), behind `ANVILLE_FIX_BRANCH=on`; about 8 Onshape calls and a ~51 s export per run ([Spike 0001](docs/spikes/0001-fix-branch-feature-edits.md), [RFC 0005](docs/rfcs/0005-fix-branch-feasibility.md)) |
| The flagged face, shown: a code-painted render, with SAM 3.1's outline only when it agrees with code | on main (PR #50, [ADR 0007 Amendment 4](docs/adrs/0007-anville-inside-onshape-identity-and-panel.md)) |
| Credits line under every comment a model worked on: which models, on which hosts | on main (PR #49) |
| Comments pinned to the flagged face | deferred past the hackathon ([RFC 0003](docs/rfcs/0003-face-anchoring.md)) |

## How it works

```
Onshape version saved
  → webhook → Modal endpoint → durable event store (SQLite on a Modal Volume, single writer)
  → STEP export → geometry (OCP): face facts, approach directions, setup plan
  → agent: plan → decide and write (Muse Spark Open) → code checks → verify (muse-spark-1.3), inside code-enforced limits
  → comment in Onshape, with a credits line; trace into the store
  → the flagged face rendered by code, outlined by SAM 3.1 when it agrees
  → (ANVILLE_FIX_BRANCH=on) the declined removal on an anville/alt-<run> branch, patch by Muse Wonder → measured → answered in the thread
reply in the thread → read by Muse Spark Open (Fireworks, Baseten as failover) → constraint → re-plan
```

- **Models:** Muse Spark Open (`meta-models/muse-spark-open-1026`, on Baseten) plans the review and decides and writes it, and reads the designer's reply on Fireworks (`accounts/fireworks/models/muse-spark-open`), with Baseten as its failover ([ADR 0011 Amendment 1](docs/adrs/0011-fireworks-host-for-spark-open.md)); `muse-spark-1.3` on the Meta Model API verifies each sentence and question the model writes against the computed facts before it's published; a question it can't support is replaced by code's own question. The fallback is each stage's fixed script: when a model call fails or runs out of time, that stage runs in code, and without the verifier the model's statements are dropped and code renders the comment. `muse-wonder-1.0` on the Meta Model API writes the fix branch's patch, and `sam-3.1` there outlines the flagged face when its mask agrees with code's. Model choice per role is decided by eval ([ADR 0004](docs/adrs/0004-model-roles-and-selection.md)); the tool-calling probe and its results are in [ADR 0003](docs/adrs/0003-model-drives-tools-compute.md) and `scripts/probe_tool_calls.py`.
- **CAD-agnostic core:** the core reads STEP and never calls a CAD API; Onshape is the first connector ([ADR 0001](docs/adrs/0001-cad-agnostic-core-with-onshape-connector.md)).
- **Decisions:** every significant choice is an ADR or RFC in `docs/`.

## Run the tests

```bash
uv sync
uv run pytest
```

Geometry tests build their synthetic parts in code (no real team CAD); the OCP wheel is about 380 MB installed.
