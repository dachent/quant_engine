"""Let the Portfolio123 twin disposition move once its version is evidenced.

Two rules froze `p123-ai-backtest-candidate` in its unresolved shape.

The first required the derived identity's span to equal the correction's
interval. That held only because admission was all-or-nothing: the identity
describes the whole derived twin, and the interval describes what the
correction admits, so once the twin is split at the strategy's own account-live
inception they are no longer the same span. The interval must now lie within
the derived span, which is the property that was actually meant -- a correction
cannot admit an interval the producer never derived.

The second refused any disposition other than REQUIRED_NOT_READY with blockers
302/303 and an UNRESOLVED version. That gate exists to stop the candidate being
flipped to admitted while its version is an assertion. It is kept for exactly
that case, and paired with the admitted shape for the case where the version is
evidenced: EXACT_MATCH, READY, no blockers, and exact-version prehistory. Both
shapes still require the same two governed receipts, and
`_verify_strategy_version_history_evidence` independently requires two captured
evidence layers before any version reference is accepted.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_financial_mvp_contract.py'
raw = path.read_bytes()

old_identity = b'''        or (
            p123_ai_identity.get("observed_start"),
            p123_ai_identity.get("observed_end"),
        )
        != (
            p123_ai_interval.get("observed_start"),
            p123_ai_interval.get("observed_end"),
        )
'''
new_identity = b'''        or not (
            str(p123_ai_identity.get("observed_start"))
            <= str(p123_ai_interval.get("observed_start"))
            <= str(p123_ai_interval.get("observed_end"))
            <= str(p123_ai_identity.get("observed_end"))
        )
'''
assert raw.count(old_identity) == 1, raw.count(old_identity)
raw = raw.replace(old_identity, new_identity)

old_gate = b'''    if (
        p123_ai_backtest["readiness"] != "REQUIRED_NOT_READY"
        or p123_ai_backtest["blockers"] != [302, 303]
        or p123_ai_backtest["strategy_version"]["status"] != "UNRESOLVED"
        or p123_ai_backtest["strategy_version"]["compatibility"] != "UNRESOLVED"
        or p123_ai_backtest["mode_admission"]
        != {"LIVE_ONLY": "NOT_APPLICABLE", "LIVE_PLUS_BACKTEST": "REQUIRED_NOT_READY"}
        or {
            "tables/returns_daily.parquet",
            "tables/strategy_relationships.parquet",
        }
        - set(p123_ai_receipts)
    ):
        raise FinancialMvpContractError(
            "P123 AI backtest bypasses its unresolved version disposition"
        )
'''
new_gate = b'''    p123_ai_version = _object(
        p123_ai_backtest["strategy_version"], "P123 AI backtest strategy_version"
    )
    unresolved_disposition = (
        p123_ai_backtest["readiness"] == "REQUIRED_NOT_READY"
        and p123_ai_backtest["blockers"] == [302, 303]
        and p123_ai_version["status"] == "UNRESOLVED"
        and p123_ai_version["compatibility"] == "UNRESOLVED"
        and p123_ai_backtest["mode_admission"]
        == {"LIVE_ONLY": "NOT_APPLICABLE", "LIVE_PLUS_BACKTEST": "REQUIRED_NOT_READY"}
    )
    admitted_disposition = (
        p123_ai_backtest["readiness"] == "READY"
        and p123_ai_backtest["blockers"] == []
        and p123_ai_version["status"] == "EVIDENCED"
        and p123_ai_version["compatibility"] == "EXACT_MATCH"
        and bool(p123_ai_version.get("version_ref"))
        and p123_ai_backtest["mode_admission"]
        == {
            "LIVE_ONLY": "NOT_APPLICABLE",
            "LIVE_PLUS_BACKTEST": "INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY",
        }
    )
    if (
        not (unresolved_disposition or admitted_disposition)
        or {
            "tables/returns_daily.parquet",
            "tables/strategy_relationships.parquet",
        }
        - set(p123_ai_receipts)
    ):
        raise FinancialMvpContractError(
            "P123 AI backtest bypasses its unresolved version disposition"
        )
'''
assert raw.count(old_gate) == 1, raw.count(old_gate)
raw = raw.replace(old_gate, new_gate)
path.write_bytes(raw)
print('ok')
