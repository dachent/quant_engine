"""The QA scripts pin rendered text; every regex pin must still match text the page can print.

Interaction QA runs against the real candidate, whose alpha tiles and other real-data text
CI's synthetic payload never renders.  A wording change in the renderer that leaves a pin
behind therefore passed CI and failed native QA (pass 30: "annualised SE" -> "annualized SE").
This test closes that gap two ways: every regex pin (``/.../.test(``) in the two QA scripts
must match somewhere in the renderer sources (app.js, index.html, the Python builders), except
the pins listed below with their reason; and the alpha-tile pin is run against the alpha tile
the renderer prints, with and without a standard error.
"""

from __future__ import annotations

import glob
from pathlib import Path
import re
from typing import Any

from test_r21_renderer import (
    _alpha_payload,
    _alpha_series,
    _context,
    _flat,
    _run,
)

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "apps" / "portfolio-control-panel"
SCRIPTS = ("interaction_qa.mjs", "browser_qa.mjs")
_PIN = re.compile(r"(?<![\w$])/((?:[^/\\\n]|\\.)+)/([gimsuy]*)\.test\(")

# Pins that are not renderer text, with the reason each is not matched against the sources.
NOT_RENDERER_TEXT = {
    r"[1-9][0-9,]* backtest": "built from a count at render time (`${n} backtest ...`); the count has no literal in the source",
    r" · observed basis": "a negative pin: the check asserts this suffix is NOT printed",
    r"\/validation\/[0-9a-f]{64}\/owner-view\.js$": "a URL test on the optional owner-view sidecar request, not page text",
    r"Failed to load resource": "Chromium console text for a failed request, not page text",
    r"^[A-Za-z][A-Za-z0-9+.-]*:": "a URL-scheme test on a request address, not page text",
    r"(?:Z|[+-]\d{2}:\d{2})$": "a timestamp-format validator on payload values, not page text",
    r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$": "a numeric-format validator on payload values, not page text",
    r"\bn \d+": "a count printed as `n <number>`; the number has no literal in the source",
    r"^Systematic HWMUnavailable": "a negative pin: the check asserts this old tile text is NOT printed",
    r"place order|submit order|execute trade|connect broker": "a negative pin: the check asserts no trading control text is printed",
    r"^(Panel verification:|Producer readiness:|Manifest self-declared readiness:)": "label text built from payload keys at render time; not verified by this test (covered only by the real-candidate run)",
    r"excess and long short factor return": "payload enum text humanised at render time; not verified by this test (covered only by the real-candidate run)",
}


def _sources() -> str:
    parts = [
        PANEL / "app.js",
        PANEL / "index.html",
        *map(Path, glob.glob(str(ROOT / "tools" / "*.py"))),
    ]
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in parts)


def _pins() -> list[tuple[str, int, str, str]]:
    found = []
    for name in SCRIPTS:
        text = (PANEL / name).read_text(encoding="utf-8")
        for m in _PIN.finditer(text):
            found.append(
                (name, text.count("\n", 0, m.start()) + 1, m.group(1), m.group(2))
            )
    return found


def test_every_regex_text_pin_in_the_qa_scripts_matches_the_renderer_sources() -> None:
    sources = _sources()
    pins = _pins()
    assert len(pins) >= 10, "the pin scan found too few pins to be reading the scripts"
    stale = []
    for name, line, pattern, flags in pins:
        if pattern in NOT_RENDERER_TEXT:
            continue
        if not re.search(pattern, sources, re.S | (re.I if "i" in flags else 0)):
            stale.append(f"{name}:{line} /{pattern}/")
    assert stale == [], f"QA pins with no matching renderer text: {stale}"


def test_every_unmatched_pin_is_listed_with_a_reason() -> None:
    pins = {pattern for _n, _l, pattern, _f in _pins()}
    assert set(NOT_RENDERER_TEXT) <= pins, (
        "an exemption no longer names a pin in the scripts"
    )


def _alpha_pin() -> re.Pattern[str]:
    text = (PANEL / "interaction_qa.mjs").read_text(encoding="utf-8")
    line = next(
        row
        for row in text.splitlines()
        if "no t-statistic" in row and "alphaTiles" in row
    )
    pattern = re.search(r"/(\([^/]*no t-statistic[^/]*)/\.test\(alphaTiles\)", line)
    assert pattern is not None, line
    return re.compile(pattern.group(1))


def test_the_alpha_tile_pin_matches_the_tile_with_and_without_a_standard_error(
    tmp_path: Path,
) -> None:
    pin = _alpha_pin()

    with_se = _alpha_series(
        _context(se="0.09", t_stat="8.99"), "AVAILABLE_OBSERVED_BASIS"
    )
    tile = _flat(
        _run(tmp_path, _alpha_payload(with_se), "renderFactorDetail"), "#factor-detail"
    )
    assert pin.search(tile), tile[:400]
    assert re.search(r"annuali[sz]ed SE", tile)

    without_se: dict[str, Any] = _alpha_series(_context(), "AVAILABLE_OBSERVED_BASIS")
    tile = _flat(
        _run(tmp_path, _alpha_payload(without_se), "renderFactorDetail"),
        "#factor-detail",
    )
    assert pin.search(tile), tile[:400]
    assert "no t-statistic" in tile
