"""The renderer and the interaction QA print/expect the same derived-value label."""

from __future__ import annotations

from pathlib import Path

APPS = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"


def test_the_interaction_qa_expects_the_label_the_renderer_prints() -> None:
    app = (APPS / "app.js").read_text(encoding="utf-8")
    qa = (APPS / "interaction_qa.mjs").read_text(encoding="utf-8")
    # one label: basis, " as of <day>", and " (observed <day>)" only when the days differ
    assert "const valueBasisLabel" in app
    for source in (app, qa):
        assert " as of " in source and "(observed " in source
    # the old "captured" wording for a derived NAV is gone from the QA expectation
    assert '["capture_date", "captured"]' not in qa
    assert '["capture_date", "captured"]' not in app
    # the derived-value surfaces all go through the one helper
    assert app.count("valueBasisLabel(") >= 2
