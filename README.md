# Anville

**A home for manufacturing infrastructure.**

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
- **The factory is part of the design space.** A fix can be a change to the part *or* to the shop. When a feature's purpose is stated (say, a chamfer that clears a weld bead), Anville won't edit it; it looks for a shop-side fix instead — a tool to buy, jaws to relieve — and checks that the cut is physically possible (in review).
- **It works in CAD, on a branch.** Comments pin to the exact face, and fixes are real feature edits on a separate workspace, never on the designer's main. Both are proven through the Onshape API; wiring them into the loop is in progress.
- **It learns in records, not weights.** Constraints, shop capabilities and outcomes are stored with provenance and fed back at the next review, so they carry over to a new base model. The ledgers exist; the loop that fills them is in progress.

## Status

| Part | State |
|---|---|
| Onshape webhook → durable event store → per-event jobs on Modal | working ([ADR 0006](docs/adrs/0006-architecture-shape-durable-event-store.md)); acceptance tests passed on staging |
| Geometry: STEP → face facts → minimum setups | working ([ADR 0008](docs/adrs/0008-geometry-stack-and-setup-count.md)); checked against Onshape's own face count on a real export |
| Edit the world and re-plan (`evaluate_edits`, `remove_feature`) | working ([ADR 0002](docs/adrs/0002-co-optimizer-edit-the-world.md)) |
| Back-chamfer cutter in relieved soft jaws: six checks incl. swept head/neck solids | in review ([RFC 0002](docs/rfcs/0002-shop-profile.md)) |
| Agent review on Muse Spark Open: a stage graph with a gated action space | in review ([ADR 0009](docs/adrs/0009-agent-loop-stage-graph.md), [RFC 0001](docs/rfcs/0001-orchestration-within-a-manufacturing-standard.md)) |
| Fix branches through the Onshape API (add, edit, suppress a feature) | proven in a spike ([Spike 0001](docs/spikes/0001-fix-branch-feature-edits.md)) |
| Reply → re-plan, shop-side proposal and setup sheet, second-part learning, docked panel | in progress |

## How it works

```
Onshape version saved
  → webhook → Modal endpoint → durable event store (SQLite on a Modal Volume, single writer)
  → STEP export → geometry (OCP): face facts, approach directions, setup plan
  → agent (Muse Spark Open): triage → options → decide → write, inside code-enforced limits
  → comment in Onshape, trace into the store
```

- **Models:** Muse Spark Open (`meta-models/muse-spark-open-1026`, on Baseten) runs the review; `muse-spark-1.3` on the Meta Model API is the fallback. Model choice per role is decided by eval ([ADR 0004](docs/adrs/0004-model-roles-and-selection.md)); the tool-calling probe and its results are in [ADR 0003](docs/adrs/0003-model-drives-tools-compute.md) and `scripts/probe_tool_calls.py`.
- **CAD-agnostic core:** the core reads STEP and never calls a CAD API; Onshape is the first connector ([ADR 0001](docs/adrs/0001-cad-agnostic-core-with-onshape-connector.md)).
- **Decisions:** every significant choice is an ADR or RFC in `docs/`.

## Run the tests

```bash
uv sync
uv run pytest
```

Geometry tests build their synthetic parts in code (no real team CAD); the OCP wheel is about 380 MB installed.
