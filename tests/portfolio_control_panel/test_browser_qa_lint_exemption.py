"""The visual lint relaxes exactly one case: a closed, deliberately clamped cell with its toggle.

The shell clamps a few dense table cells on purpose and adds a "Show more" button
(``compactDenseCells``).  The lint of ``browser_qa.mjs`` exempts such a cell, and nothing
else: a cell clipped without the toggle, a clamp class whose toggle is absent, and an opened
clamp that still clips are all flagged as ``text_overflow``.  The lint function is taken from
the script's own source, so the test cannot drift from the gate it describes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest

from browser_runtime import resolve_browser_runtime

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "apps" / "portfolio-control-panel" / "browser_qa.mjs"
RUNTIME = resolve_browser_runtime(ROOT)

pytestmark = pytest.mark.skipif(RUNTIME is None, reason="browser runtime unavailable")

_LONG = "word " * 80
_CLIP = "width: 120px; height: 24px; overflow: hidden;"


def _lint_function() -> str:
    source = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index("async function visualLint(page) {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def _findings(body: str, tmp_path: Path) -> list[dict[str, str]]:
    assert RUNTIME is not None
    runner = tmp_path / "lint_runner.mjs"
    runner.write_text(
        textwrap.dedent(
            """
            import { pathToFileURL } from "node:url";
            const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
            const body = process.argv[2];
            const browser = await chromium.launch({
              headless: true,
              ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}),
            });
            const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
            await page.setContent(`<!doctype html><html><body style="background:#fff;color:#000;font-size:14px">${body}</body></html>`);
            """
        )
        + _lint_function()
        + textwrap.dedent(
            """
            const findings = await visualLint(page);
            process.stdout.write(JSON.stringify(findings.map((item) => ({ rule: item.rule, selector: item.selector }))));
            await browser.close();
            process.exit(0);
            """
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PLAYWRIGHT_MODULE": RUNTIME.playwright_module,
        "PLAYWRIGHT_EXECUTABLE_PATH": RUNTIME.executable,
    }
    completed = subprocess.run(
        [RUNTIME.node, str(runner), body],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    parsed: list[dict[str, str]] = json.loads(completed.stdout)
    return parsed


def _overflow_findings(body: str, tmp_path: Path) -> list[dict[str, str]]:
    return [
        item for item in _findings(body, tmp_path) if item["rule"] == "text_overflow"
    ]


def test_a_closed_clamped_cell_with_its_toggle_is_exempt(tmp_path: Path) -> None:
    body = (
        f'<table><tbody><tr><td><div class="cell-clamp" style="{_CLIP}">{_LONG}</div>'
        '<button type="button" class="cell-more">Show more</button></td></tr></tbody></table>'
    )
    assert _overflow_findings(body, tmp_path) == []


def test_a_cell_clipped_without_any_toggle_is_still_flagged(tmp_path: Path) -> None:
    body = f'<table><tbody><tr><td><div style="{_CLIP}">{_LONG}</div></td></tr></tbody></table>'
    assert _overflow_findings(body, tmp_path)


def test_a_clamped_cell_whose_toggle_is_absent_is_still_flagged(tmp_path: Path) -> None:
    body = (
        f'<table><tbody><tr><td><div class="cell-clamp" style="{_CLIP}">{_LONG}</div>'
        "</td></tr></tbody></table>"
    )
    assert _overflow_findings(body, tmp_path)


def test_an_opened_clamp_that_still_clips_is_still_flagged(tmp_path: Path) -> None:
    body = (
        f'<table><tbody><tr><td><div class="cell-clamp is-open" style="{_CLIP}">{_LONG}</div>'
        '<button type="button" class="cell-more">Show less</button></td></tr></tbody></table>'
    )
    assert _overflow_findings(body, tmp_path)


def test_the_phone_navigation_strip_scrolls_sideways_by_design(tmp_path: Path) -> None:
    strip = (
        '<nav class="{cls}" style="display:flex; flex-wrap:nowrap; overflow-x:auto; '
        'width:200px;"><a href="#a" style="white-space:nowrap;flex:0 0 auto">{text}</a></nav>'
    )
    text = "navigation entry " * 20
    assert _overflow_findings(strip.format(cls="nav-stack", text=text), tmp_path) == []
    # the same scroller under any other class is still a clipped element
    assert _overflow_findings(strip.format(cls="other-strip", text=text), tmp_path)
