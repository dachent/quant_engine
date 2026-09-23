"""Keep both properties the false-design case was covering.

The case asserted that an invented product version for p123-ai-factor is
refused, and it reached that refusal through the independent-binding rule
because no source bound that strategy's version at all. Now that its sources do
bind it, an invented product version is caught earlier and more precisely, by
the source-versus-product authority rule.

Both properties still matter, so the invented-version case asserts the rule
that now decides it, and a second case strips a binding source to exercise the
independent-binding rule directly: a version bound by only one source segment,
even coherently, is not independent evidence.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/core/test_portfolio_financial_mvp_contract.py'
raw = path.read_bytes()
nl = (chr(13) + chr(10)).encode() if (chr(13) + chr(10)).encode() in raw else chr(10).encode()

old = (
    b'    with pytest.raises(' + nl
    + b'        contract_tool.FinancialMvpContractError,' + nl
    + b'        match="design/version evidence is not independently bound",' + nl
    + b'    ):' + nl
    + b'        _validate(false_design)' + nl
)
new = (
    b'    with pytest.raises(' + nl
    + b'        contract_tool.FinancialMvpContractError,' + nl
    + b'        match=(' + nl
    + b'            "source strategy design/version differs from product authority: "' + nl
    + b'            "strategy-backtests/p123-ai-factor"' + nl
    + b'        ),' + nl
    + b'    ):' + nl
    + b'        _validate(false_design)' + nl
    + nl
    + b'    # A version every source repeats is still not independent evidence unless' + nl
    + b'    # at least two segments bind it, one of them the owner account path.' + nl
    + b'    single_binding = copy.deepcopy(baseline)' + nl
    + b'    for source in single_binding["source_segments"]:' + nl
    + b'        refs = source["strategy_version"]["version_refs"]' + nl
    + b'        if source["source_id"] == "owner-account-live-history":' + nl
    + b'            continue' + nl
    + b'        refs.pop("p123-ai-factor", None)' + nl
    + b'        if not refs and source["strategy_version"]["status"] in {' + nl
    + b'            "EVIDENCED",' + nl
    + b'            "MIXED",' + nl
    + b'        }:' + nl
    + b'            source["strategy_version"]["status"] = "UNRESOLVED"' + nl
    + b'    with pytest.raises(' + nl
    + b'        contract_tool.FinancialMvpContractError,' + nl
    + b'        match="design/version evidence is not independently bound",' + nl
    + b'    ):' + nl
    + b'        contract_tool._validate_strategy_version_evidence(' + nl
    + b'            single_binding["strategies"], single_binding["source_segments"]' + nl
    + b'        )' + nl
)
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
