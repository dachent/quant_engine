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


# ---- interaction_qa.mjs:470 D8_STATUS_WORDS: real \b escapes, not U+0008 bytes -----
#
# Astra finding: D8_STATUS_WORDS held literal U+0008 backspace bytes
# (``/\x08(PUBLISHED|...)\x08/``) instead of the regex word-boundary escape
# ``\b``.  Rendered DOM text never contains a real backspace byte, so that regex
# could never match anything -- the "D8 build inputs carry no status word" check
# would silently PASS even if a status word like "PUBLISHED" leaked into the
# input-bindings text.  These two controls run the real, shipped regex (parsed by
# Node from the actual interaction_qa.mjs source, not a Python reimplementation):
# a clean binding-card text must not match, and a bindings text with "PUBLISHED"
# injected -- the exact failure mode the check exists to catch -- must match.


def _d8_status_words_source() -> str:
    text = (PANEL_ROOT / "interaction_qa.mjs").read_text(encoding="utf-8")
    marker = "const D8_STATUS_WORDS = "
    start = text.index(marker) + len(marker)
    end = text.index(";", start)
    return text[start:end]


def _d8_status_word_match(sample_text: str) -> str | None:
    import json
    import subprocess

    regex_source = _d8_status_words_source()
    script = (
        f"const re = {regex_source};"
        f"const text = {json.dumps(sample_text)};"
        "const m = re.exec(text);"
        "process.stdout.write(JSON.stringify(m ? m[1] : null));"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def test_d8_status_words_is_a_real_word_boundary_regex_with_no_backspace_bytes() -> (
    None
):
    source_bytes = (PANEL_ROOT / "interaction_qa.mjs").read_bytes()
    assert b"\x08" not in source_bytes
    assert r"\b(PUBLISHED" in _d8_status_words_source()


def test_d8_status_words_clean_control_does_not_match() -> None:
    clean = (
        "Portfolio report generation abc123456789… · sha256 def012345678…"
        " stage input, hash-bound at build"
    )
    assert _d8_status_word_match(clean) is None


def test_d8_status_words_bad_control_catches_an_injected_status_word() -> None:
    # The exact failure mode D8 exists to catch: a status word leaking into the
    # bindings text.  With the old U+0008-bracketed regex this never matched
    # (bindings text never carries real backspace bytes), so the check would
    # have silently passed on this injected literal; the fixed \b regex must
    # catch it.
    bad = (
        "Portfolio report generation abc123456789… · sha256 def012345678…"
        " PUBLISHED stage input, hash-bound at build"
    )
    assert _d8_status_word_match(bad) == "PUBLISHED"
