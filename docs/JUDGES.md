# Run Anville without CAD software

Judges can run the STEP-file connector and inspect its review, setup sheet, and trace as text/JSON. No Onshape account, CAD installation, GPU, or model key is needed for the geometry-only path below. The integrated Onshape UI and live Meta model execution are demonstrated in the submission video; they have additional credentials described below.

## 1. Install

Use Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/), then from a clone of this repository:

```bash
uv sync --locked --python 3.12
```

The OpenCascade dependency is several hundred MB installed. On Linux, if loading OCP reports missing `libGL.so.1` or X11 libraries, install your distribution's `libgl1` and `libx11-6` packages. This path runs on CPU.

## 2. Generate the synthetic demo parts

Run from the repository root. The following creates a fresh temporary directory and writes two synthetic STEP files; it does not need a CAD application or private team CAD.

```bash
uv run python - <<'PY'
from pathlib import Path
from tempfile import mkdtemp
from tests.geometry.parts import PARTS, write_step

out = Path(mkdtemp(prefix="anville-judge-"))
write_step(PARTS["demo_bracket"](), out / "part-a.step")
write_step(PARTS["demo_insert"](), out / "part-b.step")
print(f"Demo parts: {out}")
PY
```

Copy the printed directory into the next command in place of `/tmp/anville-judge-XXXX` (or use the equivalent temporary path printed on your system).

## 3. Run a real local geometry review

```bash
ANVILLE_AGENT=off uv run python scripts/anville_local.py review /tmp/anville-judge-XXXX/part-a.step --shop judge-demo
```

On PowerShell, set `$env:ANVILLE_AGENT="off"` before running the Python command instead of using the shell prefix.

Expected output includes **2 setups** on a 3-axis mill and a predicted one-setup alternative that removes the bottom chamfer. Inspect these generated files beside the STEP file:

- `part-a.review.md`: the review text.
- `part-a.sheet.json`: the setup-sheet data.
- `part-a.trace.jsonl`: the execution trace.

This executes the production local connector and geometry code. With `ANVILLE_AGENT=off`, it deliberately makes no model calls: it does not reproduce live Meta reasoning, SAM segmentation, or the Onshape interface. It predicts a written alternative rather than editing a CAD branch. No claim is made that this offline review physically machined a part.

## 4. Verify the recorded-knowledge loop without credentials

```bash
uv run pytest -q tests/geometry/test_demo_parts.py tests/geometry/test_demo_insert.py tests/agent/test_reply.py tests/connectors/test_local.py
```

These tests generate synthetic geometry and verify:

- The bottom chamfer causes the additional setup, and removing it predicts the measured setup change.
- Designer constraints protect a required feature and guide the shop-side plan.
- A shop note records ownership of cutter 910480, and the next part uses that capability.
- The local connector persists reviews, replies, setup sheets, traces, and shop records.

The tests use scripted model responses where an agent response is needed. They exercise the harness and persistence without consuming API credits; they are not a replay of fresh live model inference. For the actual recorded Meta-model run and its UI behavior, see the submitted video.

## Optional: live Meta reasoning on local STEP files

Copy `.env.example` to `.env`, provide your own provider credentials, and keep `.env` private. The configured roles are documented in the main README: `MUSE_SPARK_OPEN_BASE_URL`, `MUSE_SPARK_OPEN_API_KEY`, and `MUSE_SPARK_OPEN_MODEL_ID` for the open-weight model; `MODEL_API_KEY` for the Meta API verifier and other configured Meta stages; Fireworks credentials are optional for its reply-reader route.

Run the same review command without `ANVILLE_AGENT=off`. For PowerShell, remove the variable with `Remove-Item Env:ANVILLE_AGENT` first. With no keys, the CLI reports and uses its geometry-only fallback. Model-enabled runs use your credits and may select different options than the recorded run.

The local CLI also supports:

```bash
uv run python scripts/anville_local.py reply /tmp/anville-judge-XXXX/part-a.step "The chamfer is required for welding clearance." --shop judge-demo
uv run python scripts/anville_local.py shop-note /tmp/anville-judge-XXXX/part-a.step --arrived 910480 --shop judge-demo
uv run python scripts/anville_local.py review /tmp/anville-judge-XXXX/part-b.step --shop judge-demo
```

Use the same shop name and directory to share its ledger. The arrival command only accepts cutters that a prior review actually decided to buy; the geometry-only fallback buys none. The second-part result therefore depends on the recorded plan and acquisition, not merely on running these commands. The synthetic tests above verify that path deterministically.

## Full Onshape integration

The video uses the deployed Onshape connector, docked panel, comments, and alternative branch. Reproducing that integration requires an Onshape account with API/webhook access and editable synthetic documents, a Modal account/deployment, provider credentials, and the values in `.env.example`. It is not an anonymous public CAD sandbox.

Deployment entry point: `anville/modal_app.py` (its module header documents deployment and webhook registration). Panel and OAuth configuration: `docs/adrs/0007-anville-inside-onshape-identity-and-panel.md`. For a quick software-only assessment, use the local review and tests above.
