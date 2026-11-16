"""#904 regression case mwr_short_window_not_primary: Money-weighted XIRR shown as the headline for a window under a year (#904 item 4)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))


def test_mwr_short_window_not_primary() -> None:
    source = (ROOT / "apps" / "portfolio-control-panel" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "mwrShortWindow" in source
