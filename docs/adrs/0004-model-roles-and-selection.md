# ADR 0004: Model roles, providers, and how a model is chosen per role

- **Status:** Proposed
- **Date:** 2026-10-26
- **Supersedes:** locked decision 5 (model roles) in the project brief, in part. "Muse Code writes the fix" is replaced, because Muse Code is a subscription product and not a callable model.
- **Depends on:** ADR 0003

## Context

Meta's model docs (dev.meta.ai, checked 2026-10-26) establish these facts:

- **Meta Model API** (`https://api.meta.ai/v1`, OpenAI-compatible, bearer `MODEL_API_KEY`):
  - `muse-spark-1.3`, `-1.2`, `-1.1`: text, image, video, PDF in; tool calling; 1M-token context. $1.25 in / $4.25 out per 1M tokens.
  - `-contributor` variants: cheaper, but prompts and outputs may be used to train Meta models.
  - `sam-3.1`: segmentation, $2.50 per 1,000 images.
- **Muse Spark Open** (816B, open weights) is **not** on Model API. Meta's deploy page lists the generic ID `meta-models/muse-spark-open`. The hackathon's Baseten deployment is `meta-models/muse-spark-open-1026` at `https://inference.baseten.co/v1`, and the hackathon key is scoped to it: the generic ID returns 403 (verified 2026-10-26; the `-1026` ID returned 200 with a valid tool call). Self-hosting needs 4× B300, which is out of reach.
- **Muse Glimmer 30B** (Apache 2.0) is self-host only. Its 4-bit variant fits 24 GB of VRAM (Hugging Face model card). The card claims tool use but documents no tool-call parser or structured-output mode.
- **Muse Code** is a product (a coding agent), not a model ID. It can be driven programmatically through `muse-code-sdk` (TypeScript, Developer Preview; needs Node 20 and the `muse` host binary).
- **Structured output:** Chat Completions accepts `response_format: {type: "json_schema"}`. The Responses API uses `text.format` instead. Recursive schemas are not supported.

Capability and cost differ by role, and claims about which model is better are vendor-reported. The choice should therefore be **measured**, not assumed.

## Decision

1. **Roles and candidates.** Every role sits behind one OpenAI-compatible client, so a provider swap is a config change.

   | Role | Primary | Fallback |
   |---|---|---|
   | Agent loop (ADR 0003) | Muse Spark Open (hosted) | `muse-spark-1.3` |
   | Fix compiler: intent → Onshape feature JSON | Muse Spark Open (hosted) | `muse-spark-1.3` |
   | Geometry anchoring on renders | `sam-3.1` (Model API) | none (the flag stays text-only) |
   | Reply parsing | **pending a decision** (Muse Glimmer on Modal is a candidate) | `muse-spark-1.3` |

2. **Each role's primary model is picked by a small eval, not by reputation.** Every candidate runs the same fixture tasks:
   - tool-call validity (2 tools, JSON schema)
   - fix-JSON validity on synthetic intents
   - reply-parse accuracy on recorded replies

   Each run records valid-or-correct rate, p50 latency, and dollars per task. **Rule: the cheapest candidate that clears the role's threshold wins.** The default threshold is at least 9 of 10 valid. Results are recorded in the repo.
3. **Standard mode only.** No `-contributor` model IDs anywhere: development, tests, or demo. Prompts and outputs are not used for Meta training, and `max` reasoning stays available.
4. **Muse Spark Open runs as `meta-models/muse-spark-open-1026` on Baseten** (corrected: an earlier draft of this ADR replaced it with the generic ID, which the hackathon key rejects). The ID is configuration (`MUSE_SPARK_OPEN_MODEL_ID`), not code.
5. **Structured outputs where the role allows it.** Extraction roles (fix JSON, reply parsing, ADR 0005 evidence) request `json_schema` output, and code still validates the result. The agent loop uses tool calls.
6. **Muse Code is an escalation path for the fix compiler, not a primary role.** If the fix compiler fails its eval, a Muse Code session driven through `muse-code-sdk` is the next candidate. It stays out of the hot path because it adds a Node runtime and a preview SDK.

## Options considered

1. **Muse Spark Open everywhere.** Strongest open-weights story, but a single dependency on hosted credits.
2. **`muse-spark-1.3` everywhere.** Simplest and fully covered by Meta credits, but no open-weights path.
3. **Per-role primary and fallback, selected by eval (chosen).**

## Trade-offs

- Two providers to configure and two prompt variants to keep working per role.
- The eval costs some tokens and about an hour up front. It also produces evidence of which model does what.

## Consequences

- **Easier:** a missing or expensive host never blocks the demo, because there is always a fallback. Model claims in the writeup are backed by measured numbers.
- **Harder:** any prompt change has to re-run the eval for that role.
- **Revisit:** when hosted Muse Spark Open pricing or availability changes, or when the reply-parsing role is decided.
