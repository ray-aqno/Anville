"""Runs the panel's JS tests (reducer, pacing, view) under `node --test`, so `pytest` covers them too."""

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def test_panel_js_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed: run `node --test tests/panel/` where it is")
    files = sorted(str(p) for p in HERE.glob("*.test.mjs"))
    r = subprocess.run([node, "--test", *files], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
