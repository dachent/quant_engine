"""D11 kill test: the panel no longer says no provider/model comparison was accepted."""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "apps/portfolio-control-panel/app.js"


def test_the_untrue_provider_comparison_fallback_is_gone() -> None:
    text = APP.read_text(encoding="utf-8")
    assert "No accepted exact-date provider/model comparison" not in text
    assert "Model live vs My live comparison not published" in text
