"""#540 browser-load harness: verdict arithmetic and an end-to-end run on a stub page."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from browser_runtime import resolve_browser_runtime
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY_ROOT / "apps" / "portfolio-control-panel" / "measure_load.mjs"

_UNIT = """
import { summarize, verdict } from %(harness)s;
const run = (ms) => ({ usable_load_ms: ms });
const cand = summarize([run(2000), run(2200), run(2400)]);
const ctl = summarize([run(2000), run(2000), run(2000)]);
process.stdout.write(JSON.stringify({
  cand,
  ok: verdict(cand, ctl, 1.25),
  bad: verdict(cand, ctl, 1.05),
  none: verdict(cand, null, 1.25),
}));
"""

_STUB = '<!doctype html><div id="app" data-app-state="ready"></div>'


def test_summary_and_verdict_compare_candidate_to_control() -> None:
    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("node is unavailable")
    source = _UNIT % {"harness": json.dumps(HARNESS.as_uri())}
    done = subprocess.run(
        [runtime.node, "--input-type=module", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(done.stdout)
    assert out["cand"]["mean_ms"] == 2200
    assert out["ok"] == {"ratio": 1.1, "max_ratio": 1.25, "pass": True}
    assert out["bad"]["pass"] is False
    assert out["none"] == {"ratio": None, "pass": None}


def test_harness_measures_a_stub_panel_with_a_control(tmp_path: Path) -> None:
    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("browser runtime is unavailable")
    page = tmp_path / "index.html"
    page.write_bytes(_STUB.encode("utf-8"))
    out = tmp_path / "result.json"
    done = subprocess.run(
        [
            runtime.node,
            str(HARNESS),
            "--html",
            str(page),
            "--control",
            str(page),
            "--runs",
            "2",
            "--idle-wait",
            "0",
            "--max-ratio",
            "100",
            "--port",
            "9341",
            "--out",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PLAYWRIGHT_MODULE": runtime.playwright_module,
            "PLAYWRIGHT_EXECUTABLE_PATH": runtime.executable,
        },
        timeout=240,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(out.read_bytes())
    assert len(report["candidate"]["timed"]) == 2
    assert report["verdict"]["pass"] is True
    assert report["candidate"]["errors"] == []
