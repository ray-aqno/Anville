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
- **It works in CAD, on a branch.** Fixes are real feature edits on a separate workspace, never on the designer's main: proven through the Onshape API in a spike, not yet wired into the loop ([RFC 0005](docs/rfcs/0005-fix-branch-feasibility.md), draft). Pinning each comment to the exact face is proposed: matched offline, with the live probe still to run ([RFC 0003](docs/rfcs/0003-face-anchoring.md), draft).
- **It learns in records, not weights.** Constraints, shop capabilities and outcomes are stored with provenance; the designer's constraints and the tools the shop has bought are fed back at the next review, so they carry over to a new base model. A designer's reply becomes a constraint (on main); a shop's note, sent from the setup sheet's traveler page, becomes an outcome. Both are on main; the first live end-to-end run is in progress.

## Status

| Part | State |
|---|---|
| Onshape webhook → durable event store → per-event jobs on Modal | working ([ADR 0006](docs/adrs/0006-architecture-shape-durable-event-store.md)); acceptance tests passed on staging |
| Geometry: STEP → face facts → minimum setups | working ([ADR 0008](docs/adrs/0008-geometry-stack-and-setup-count.md)); checked against Onshape's own face count on a real export |
| Edit the world and re-plan (`evaluate_edits`, `remove_feature`) | working ([ADR 0002](docs/adrs/0002-co-optimizer-edit-the-world.md)) |
| Back-chamfer cutter in relieved soft jaws: six checks incl. swept head/neck solids | working ([RFC 0002](docs/rfcs/0002-shop-profile.md), closed: implemented in `anville/shop`) |
| Agent review: plan and decide-and-write on Muse Spark Open, each sentence and question the model writes verified by `muse-spark-1.3` (code renders the rest), inside a gated action space | working ([ADR 0009](docs/adrs/0009-agent-loop-stage-graph.md)); an eval gate on the live models, scored in code (PR #31) |
| Shop-side fixes, priced and ranked: buying a cutter, with its payback; costs checked at every corner of the shop's input ranges | working ([ADR 0010](docs/adrs/0010-cost-ranked-any-intent-fixes.md)) |
| The designer's reply in the thread → a constraint → a re-plan | on main ([ADR 0012](docs/adrs/0012-reply-to-replan.md)); first live end-to-end run in progress |
| Setup sheet stored with each version's review and linked from its comment when the panel is mounted (a re-plan doesn't make a new sheet); the shop's traveler page and its notes → the shop ledger; the next part planned with what the shop owns | on main (PRs #40, #42, #44); first live end-to-end run in progress |
| Docked Onshape panel: live loop ring, decision card, model chips | on main ([ADR 0007](docs/adrs/0007-anville-inside-onshape-identity-and-panel.md)); mounted once its token is set (and differs from the webhook and OAuth admin secrets) |
| Fix branches through the Onshape API (add, edit, suppress a feature) | proven in a spike ([Spike 0001](docs/spikes/0001-fix-branch-feature-edits.md)); not in the loop ([RFC 0005](docs/rfcs/0005-fix-branch-feasibility.md), draft) |
| Comments pinned to the flagged face | proposed ([RFC 0003](docs/rfcs/0003-face-anchoring.md), draft) |

## How it works

```
Onshape version saved
  → webhook → Modal endpoint → durable event store (SQLite on a Modal Volume, single writer)
  → STEP export → geometry (OCP): face facts, approach directions, setup plan
  → agent: plan → decide and write (Muse Spark Open) → code checks → verify (muse-spark-1.3), inside code-enforced limits
  → comment in Onshape, trace into the store
```

- **Models:** Muse Spark Open (`meta-models/muse-spark-open-1026`, on Baseten) plans the review and decides and writes it; `muse-spark-1.3` on the Meta Model API verifies each sentence and question the model writes against the computed facts before it's published; a question it can't support is replaced by code's own question. The fallback is each stage's fixed script: when a model call fails or runs out of time, that stage runs in code, and without the verifier the model's statements are dropped and code renders the comment. Model choice per role is decided by eval ([ADR 0004](docs/adrs/0004-model-roles-and-selection.md)); the tool-calling probe and its results are in [ADR 0003](docs/adrs/0003-model-drives-tools-compute.md) and `scripts/probe_tool_calls.py`.
- **CAD-agnostic core:** the core reads STEP and never calls a CAD API; Onshape is the first connector ([ADR 0001](docs/adrs/0001-cad-agnostic-core-with-onshape-connector.md)).
- **Decisions:** every significant choice is an ADR or RFC in `docs/`.

## Run the tests

```bash
uv sync
uv run pytest
```

Geometry tests build their synthetic parts in code (no real team CAD); the OCP wheel is about 380 MB installed.
