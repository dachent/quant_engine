from acceptance.validate.shell import payload_dereferences_before_guard


def test_optional_chain_in_true_branch_is_safe() -> None:
    js = """
const data = window.PORTFOLIO_DATA;
const factorLensModes = data?.decomposition_lenses?.modes || {
  LIVE_ONLY: data?.decomposition_lenses?.lenses || [data?.factor_analysis]
};
let selectedFactorEvidenceMode =
    factorLensModes[data?.decomposition_lenses?.default_evidence_mode]
      ? data?.decomposition_lenses?.default_evidence_mode
    : "LIVE_ONLY";
"""

    assert payload_dereferences_before_guard(js, {"PORTFOLIO_DATA"}) == []


def test_eager_dereference_before_later_guard_remains_a_finding() -> None:
    js = """
const data = window.PORTFOLIO_DATA;
const selected = data.rows.length;
if (!data) throw new Error("missing data");
"""

    findings = payload_dereferences_before_guard(js, {"PORTFOLIO_DATA"})
    assert len(findings) == 1
    assert findings[0].binding == "data"


def test_optional_chain_with_fallback_does_not_guard_unsafe_ternary_branch() -> None:
    js = """
const data = window.PORTFOLIO_DATA;
const selected = data?.rows || [] ? data.rows.length : 0;
"""

    findings = payload_dereferences_before_guard(js, {"PORTFOLIO_DATA"})

    assert len(findings) == 1
    assert findings[0].binding == "data"


def test_direct_optional_chain_ternary_guards_its_true_branch() -> None:
    js = """
const data = window.PORTFOLIO_DATA;
const count = data?.rows ? data?.rows.length : 0;
"""

    assert payload_dereferences_before_guard(js, {"PORTFOLIO_DATA"}) == []


def test_comparison_with_optional_chain_does_not_guard_true_branch() -> None:
    js = """
const data = window.PORTFOLIO_DATA;
const count = undefined === data?.rows ? data.rows.length : 0;
"""

    findings = payload_dereferences_before_guard(js, {"PORTFOLIO_DATA"})

    assert len(findings) == 1
    assert findings[0].binding == "data"


def test_reassignment_inside_true_branch_does_not_preserve_optional_chain_guard() -> (
    None
):
    js = """
let data = window.PORTFOLIO_DATA;
const count = data?.rows ? (data = null, data.rows.length) : 0;
"""

    findings = payload_dereferences_before_guard(js, {"PORTFOLIO_DATA"})

    assert len(findings) == 1
    assert findings[0].binding == "data"
