"""Batch V8: the F2 scanner and a true branch keyed by the very lookup it reads.

The shell reads ``modes[data?.x?.y] ? data.x.y : "LIVE_ONLY"``: with no payload the key is
``undefined``, the lookup is falsy, and the dereferencing branch never runs; ``init()``
then reaches its own throw-in-try guard and the error state is shown.  F2 was disproved by
running the generation's ``app.js`` with no payload.  A dereference that is not keyed by
its own lookup is still an eager dereference.
"""

from acceptance.validate.shell import payload_dereferences_before_guard

GLOBALS = {"PORTFOLIO_DATA"}

SHELL = """
const data = window.PORTFOLIO_DATA;
const modes = data?.decomposition_lenses?.modes || { LIVE_ONLY: [] };
let selected = modes[data?.decomposition_lenses?.default_evidence_mode]
  ? data.decomposition_lenses.default_evidence_mode : "LIVE_ONLY";
const dependenceModes = data?.dependence_modes || { LIVE_ONLY: data?.dependence_analysis };
let mode = dependenceModes[data?.default_dependence_mode] ? data.default_dependence_mode : "LIVE_ONLY";
function init() {
  try {
    if (!data || data.panel_schema_version !== 22) throw new Error("missing");
    document.title = data.meta.title;
  } catch (error) { showAppError(error); }
}
document.addEventListener("DOMContentLoaded", init);
"""


def test_v8_a_branch_that_reads_the_path_its_lookup_keyed_is_not_eager() -> None:
    assert payload_dereferences_before_guard(SHELL, GLOBALS) == []


def test_v8_a_branch_that_reads_a_different_path_is_still_eager() -> None:
    js = SHELL.replace("? data.default_dependence_mode :", "? data.other_mode.value :")
    findings = payload_dereferences_before_guard(js, GLOBALS)
    assert [f.binding for f in findings] == ["data"]
    assert findings[0].line == 7


def test_v8_a_lookup_keyed_by_an_unguarded_read_is_still_eager() -> None:
    js = SHELL.replace(
        "dependenceModes[data?.default_dependence_mode]",
        "dependenceModes[data.default_dependence_mode]",
    )
    findings = payload_dereferences_before_guard(js, GLOBALS)
    assert [f.binding for f in findings] == ["data"]
    assert findings[0].line == 7


def test_v8_a_plain_eager_dereference_is_still_eager() -> None:
    js = SHELL + "const rows = data.rows.length;\n"
    assert [f.binding for f in payload_dereferences_before_guard(js, GLOBALS)] == [
        "data"
    ]


def test_v8_the_keyed_branch_does_not_excuse_a_later_eager_dereference() -> None:
    js = SHELL.replace(
        "function init()", "const n = data.rows.length;\nfunction init()"
    )
    findings = payload_dereferences_before_guard(js, GLOBALS)
    assert [f.line for f in findings] == [8]
