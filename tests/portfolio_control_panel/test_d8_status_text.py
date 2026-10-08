"""D8: the panel renders no positive status word from a literal (status text ruling)."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from acceptance.validate import checks, shell  # noqa: E402

PANEL_ROOT = REPOSITORY_ROOT / "apps" / "portfolio-control-panel"
NL = chr(10)

# Verbatim qc1-main e6392f3 apps/portfolio-control-panel/app.js line 1567.
QC1_MAIN_VALIDATION_CARDS_LINE = """    $("#validation-cards").innerHTML = `<div class="validation-card"><div><strong>Portfolio report</strong><span>${safe(data.meta.report_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Factor / alpha</strong><span>${safe(data.meta.decomposition_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Dependence</strong><span>${safe(data.meta.correlation_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Frozen candidate appendix</strong><span>${safe(data.candidate_appendix.as_of_date)} · hash locked</span></div><div class="validation-score">FROZEN</div></div><div class="callout callout-neutral">Portfolio, analytics, and appendix generations retain separate authority. Candidate rows never enter current strategy totals.</div>`;"""


def _app_js() -> str:
    return (PANEL_ROOT / "app.js").read_bytes().decode("utf-8")


def _index_html() -> str:
    return (PANEL_ROOT / "index.html").read_bytes().decode("utf-8")


def _findings(app_js: str, index_html: str = "") -> list[checks.Finding]:
    """Run the real check_hardcoded_status over a sealed-panel stand-in (state STAGED)."""

    context = SimpleNamespace(
        gen=SimpleNamespace(
            candidate={"state": "STAGED"}, app_js=app_js, index_shell=index_html
        ),
        panel={},
    )
    return list(checks.check_hardcoded_status(context))  # type: ignore[arg-type]


def test_the_real_qc1_main_validation_cards_line_is_one_hardcoded_status_finding() -> (
    None
):
    app_js = (
        "function render(data) {" + NL + QC1_MAIN_VALIDATION_CARDS_LINE + NL + "}" + NL
    )

    found = _findings(app_js)

    assert [item.check_id for item in found] == ["code.hardcoded_status_literal"]
    assert found[0].klass == checks.BLOCKER_AUTHORITY
    assert found[0].observed["literal"] == "PUBLISHED"
    assert found[0].observed["context"] == "text-node"


def test_the_shipped_panel_templates_carry_no_hardcoded_status_finding() -> None:
    assert _findings(_app_js(), _index_html()) == []
    assert shell.hard_coded_statuses("app.js", _app_js()) == []
    assert shell.hard_coded_statuses("index.html", _index_html()) == []


def test_the_readiness_source_exemption_covers_no_positive_literal_in_app_js() -> None:
    """shell.py skips any interpolation mentioning readiness, publication, verification
    or release_state.  Pin that this exemption hides nothing in the shipped app.js: with
    the exemption disabled the scanner still finds zero positive literals.
    """

    text = _app_js()
    original = shell._READINESS_SOURCE
    shell._READINESS_SOURCE = re.compile(r"(?!)")
    try:
        uses = shell.hard_coded_statuses("app.js", text)
    finally:
        shell._READINESS_SOURCE = original

    assert uses == []


def test_the_old_validation_card_and_readiness_wording_is_gone() -> None:
    text = _app_js()
    html = _index_html()

    assert "#validation-cards" not in text
    assert 'id="validation-cards"' not in html
    assert 'id="input-bindings"' in html
    for old in ("Panel verification:", "Producer readiness:", "Manifest self-declared"):
        assert old not in text
