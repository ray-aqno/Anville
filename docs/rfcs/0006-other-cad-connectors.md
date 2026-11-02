# RFC 0006: Connectors beyond Onshape: what maps, what degrades

- **Status:** Draft
- **Date:** 2026-11-02
- **Builds on:** ADR 0001 (CAD-agnostic core with an Onshape connector), ADR 0007 (inside Onshape), RFC 0005 (fix branch)

## Problem

Onshape is the hackathon surface, but Anville is meant to be a product: a manufacturing engineer that sits wherever design work happens. ADR 0001 split the system into a core and a connector, but only one connector exists, and `LocalSource`/`LocalSink` were never built. This RFC asks two questions:
- What exactly would a second CAD need to provide?
- Which of today's features carry over unchanged, which degrade, and which need that CAD's own API?

## Constraints

- **Locked decision 3:** setup count is computed from geometry. Every connector must deliver a solid the core can measure. Today that's STEP.
- **Locked decision 4:** learning lives in stored, provenance-tagged records, not in the CAD.
- The core must not import a CAD SDK. A connector may depend on the core, never the reverse.
- **No regressions to the Onshape path.** It is the demo.

## Where the seams are today (verified against main d51173e, 2026-11-02)

The core reads STEP and shop data only. These modules import nothing from a CAD:
- `anville/geometry/*`: facts, access, setup plan, back-chamfer checks, `describe_group`, renders, SAM anchoring;
- `anville/shop/*`: profile, cost, rank, sheet, ledger;
- `anville/agent/review.py`: plan, decide and verify.

Onshape reaches the core in four places:

| Place | What it does | Onshape-specific because |
|---|---|---|
| `connectors/onshape/events.py`, `modal_app.py` webhook | The trigger: a version was saved, someone commented, a translation finished | Onshape webhooks |
| `modal_app._review` export | Gets the version's STEP | Onshape Translations API |
| `connectors/onshape/oauth.post_as_anville` (+ `reply.KEYS_HEADER`) | Posts the review and replies in its own identity; recognises its own comments | Onshape comments and OAuth |
| `connectors/onshape/fixbranch.build_branch` (called from `job.fix_branch`) | Builds the declined removal on a branch and re-measures it | Onshape workspaces, FeatureScript, feature API |

Plus the panel, a web page embedded through an Onshape extension (ADR 0007). The panel itself only reads Anville's trace by document id (`panel/router.py`).

## The interfaces a connector implements

Named after ADR 0001, plus the two seams that the live build added:

1. **`CadSource`:** emits `version_saved(doc, version, step_bytes)`, `comment_added(doc, thread, text, author)` and, optionally, `note_added`. It must provide a stable version identity and the version's STEP.
2. **`ReviewSink`:** `post(doc, version, text) -> thread_id`, `reply(thread_id, text)`, and `is_own(author)` (the self-loop filter, ADR 0003 F1).
3. **`CadEditor`** (optional): `apply(intent, on_branch) -> step_bytes | None`. Fix intents are already CAD-neutral, e.g. "remove face group G11". Without an editor, the intent is shown as a written fix (ADR 0001's fallback).
4. **`Presence`** (optional): where the panel URL is embedded. Without one, the panel opens as a browser window beside the CAD, as it ran before the Onshape extension existed.

## What carries over, per feature

| Feature | Needs from the CAD | Carries over to any CAD that exports STEP? |
|---|---|---|
| Setup count, reach, back-chamfer checks | STEP | **Yes, unchanged** |
| Costed options, ranking, payback | Shop profile | **Yes, unchanged** |
| Model review, verify, credits line | Nothing | **Yes, unchanged** |
| Plain face names (`describe_group`) | STEP | **Yes, unchanged** |
| SAM-outlined render | STEP (code renders it) | **Yes, unchanged** |
| Setup sheet, shop note, learning beat | Nothing (Anville's own pages and ledger) | **Yes, unchanged** |
| Trigger on save | A save or check-in event | Needs `CadSource` |
| Review and thread replies | Somewhere to post and read text | Needs `ReviewSink`. Fallback: the panel thread (`/api/ask`, specced, not built) or Anville's own review page |
| Fix branch, measured | A programmatic edit API and branching | Needs `CadEditor`. Fallback: the written fix plus the predicted count (`job.removal_alternative`) |
| Docked panel | An embeddable web view | Needs `Presence`. Fallback: a browser window |

So 6 of the 10 features need nothing from the CAD beyond STEP. The other four degrade to working fallbacks, so no feature disappears.

## Options for the next connector

The host details below are **prior knowledge, not checked against current vendor docs.** Check each one before building.

1. **Local folder (`LocalSource`/`LocalSink`, decided in ADR 0001).**
   - A STEP file dropped in a folder triggers a review. The review is written beside it, replies come from a CLI, and the shop ledger is a local file.
   - Works with **any** CAD that exports STEP, today, with no vendor API.
   - It's the reference connector and the test harness for the others. Being built now (panel lane).
2. **PDM/PLM check-in** (e.g. SolidWorks PDM, Windchill, Teamcenter).
   - These vaults can run workflow actions on check-in, which gives the trigger.
   - Review notes or change requests give the thread. STEP comes from a vault export or a conversion task.
   - It meets most mechanical teams where releases already happen.
3. **Desktop CAD add-in** (SolidWorks, Inventor, Fusion).
   - An add-in can host a web view, which gives the panel (`Presence`).
   - Its API can make feature edits on a configuration or branch, which gives a `CadEditor`.
   - It's the richest option and the most per-vendor work.
4. **Git for CAD** (STEP files in a repository).
   - A push triggers a review, and pull-request comments are the thread.
   - Cheap, but niche in mechanical teams.

## Open questions

- **STEP fidelity across exporters:** do face counts and group labels stay stable between versions from the same CAD? Onshape's do, verified on DEMO A v1/v2/v3. SolidWorks, Fusion and Inventor are unverified.
- **Identity:** in a vault, who is "Anville"? A service account per tenant, or the reviewer's own account plus a marker?
- **Face pinning without Onshape's ids:** RFC 0003 is closed for the demo. A `CadEditor` that edits faces needs our face-to-entity map. `geometry/anchor.py` already does centroid matching, which is CAD-neutral.
- **The panel thread** (`/api/ask`): build it once, and every CAD without native comments gets the full reply → re-plan loop.

## Recommendation

1. **Build `LocalSource`/`LocalSink` now** (in progress), as ADR 0001 decided. It proves the core runs end to end on a STEP from any CAD: review, reply → re-plan, shop note → learning. It also becomes the conformance test that every future connector must pass.
2. **Keep Onshape as the only live connector through the hackathon.**
3. **After it, choose the second connector by customer pull.**
   - My lean is PDM check-in (option 2): the trigger and thread live where releases happen, and it needs no desktop install.
   - Then a desktop add-in for `Presence` and `CadEditor`.
4. **Build the panel's reply thread** (`/api/ask`), so that no connector loses the conversation loop.

When this is decided, it closes with an ADR. Rayyan accepts it.
