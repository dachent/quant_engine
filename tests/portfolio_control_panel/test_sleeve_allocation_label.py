"""N36: a sleeve allocation is labelled as an allocation when no governed invested
capital reached the build.

OWNER_318: a strategy's capital is its sleeve NAV plus the unallocated cash line,
and the allocation to the sleeve is not invested capital.  The manifest table
column is "Capital in" (opening capital + deposits + internal-in, never governed
invested capital); the row title and the legend name the missing
governed input instead of promising a separate report that was never supplied.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

APP = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"
APP_JS = (APP / "app.js").read_text(encoding="utf-8")
QA_JS = (APP / "interaction_qa.mjs").read_text(encoding="utf-8")


def test_the_sleeve_label_depends_on_the_governed_capital_result() -> None:
    assert "const governedCapitalSupplied = Boolean(data.invested_capital);" in APP_JS
    assert (
        "Governed invested capital was not supplied to this build, so this is an "
        "allocation, not invested capital."
    ) in APP_JS
    assert (
        "Governed invested capital was not supplied to this build: on these rows "
        "Capital in is an allocation to the strategy sleeve, not invested capital."
    ) in APP_JS


def test_the_separate_report_claim_only_appears_when_the_input_was_supplied() -> None:
    sentence = (
        "Governed invested capital (the deposits) and the strategy cash line are "
        "reported separately."
    )
    assert APP_JS.count(sentence) == 1
    assert f'governedCapitalSupplied ? "{sentence}"' in APP_JS


def test_the_column_is_capital_in_and_the_qa_header_list_agrees() -> None:
    assert '["Capital in", "numeric", sleeveHeader]' in APP_JS
    assert '["Invested", "numeric"' not in APP_JS
    assert '"Observed interval", "Capital in", "Withdrawn"' in QA_JS
    assert "Capital in = explicit opening capital + deposits + internal-in" in APP_JS
    assert "Net capital = Capital in − Withdrawn." in APP_JS


def test_the_attended_qa_asserts_the_rendered_capital_legend_sentences() -> None:
    """The legend is read from the DOM, so both definitions must be asserted there."""

    assert "epochLegend.includes(\"Capital in = explicit opening capital" in QA_JS
    assert "epochLegend.includes(\"Net capital = Capital in − Withdrawn.\")" in QA_JS
    # The same sentences the renderer writes, so QA and app.js cannot drift apart.
    for sentence in (
        "Capital in = explicit opening capital + deposits + internal-in after start.",
        "Net capital = Capital in − Withdrawn.",
    ):
        assert sentence in APP_JS
        assert sentence in QA_JS


def test_the_performance_table_marks_an_unproven_flow_path() -> None:
    assert "row.mwr_flow_path_status" in APP_JS
    assert "flow path unproven" in APP_JS
    for status in (
        "NO_ACCEPTED_FACT_PATH",
        "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF",
    ):
        assert f'"{status}"' in APP_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_app_js_still_parses() -> None:
    result = subprocess.run(
        ["node", "--check", str(APP / "app.js")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
