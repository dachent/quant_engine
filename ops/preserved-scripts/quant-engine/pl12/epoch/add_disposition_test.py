"""Add a reader-side test for the deprecated retained-history disposition.

The history builder has always written `RETAINED_REJECTED_HISTORY_DEPRECATED`
for EchoTrade and the bundle reader's allow-list never listed it, so every
bundle carrying that disposition was rejected outright. The writer side was
asserted; the reader side was not, which is exactly why the seam held a
release-blocking defect with a green suite.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/portfolio_analytics/test_portfolio_target_series.py'
raw = path.read_bytes()
nl = b'\r\n' if b'\r\n' in raw else b'\n'

test = '''

def _history_bundle(tmp_path, dispositions):
    """A verified bundle carrying one admitted series and the dispositions."""

    returns = tmp_path / "history-returns.csv"
    returns.write_text(
        "series_id,date,return,evidence_layer,strategy_version"
        + chr(10)
        + "Alpha,2025-12-30,0.02,BACKTEST,alpha-current:v1"
        + chr(10)
        + "Alpha,2025-12-31,-0.01,BACKTEST,alpha-current:v1"
        + chr(10),
        encoding="utf-8",
        newline="",
    )
    manifest = tmp_path / "history-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-06",
                "builder": "portfolio-strategy-history",
                "builder_version": "1.6.0",
                "bundle_id": "b" * 64,
                "history_dispositions": dispositions,
                "returns_artifact": {
                    "artifact_id": "strategy-history/returns.csv",
                    "sha256": hashlib.sha256(returns.read_bytes()).hexdigest(),
                    "size_bytes": returns.stat().st_size,
                },
                "schema_version": 1,
                "series": [
                    {
                        "compatibility_basis": "SYNTHETIC_CURRENT_SPECIFICATION",
                        "frequency": "DAILY_TRADING",
                        "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                        "implementation_reconciliation_status": "UNRESOLVED",
                        "observed_end": "2025-12-31",
                        "observed_start": "2025-12-30",
                        "required_for_analysis": True,
                        "return_observations": 2,
                        "series_id": "Alpha",
                        "strategy_version": "alpha-current:v1",
                    }
                ],
                "verification": "VERIFIED",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest, returns


DEPRECATED_DISPOSITION = {
    "history_status": "RETAINED_REJECTED_HISTORY_DEPRECATED",
    "implementation_reconciliation_status": "NOT_APPLICABLE_DEPRECATED",
    "reason": (
        "EchoTrade is deprecated; retained event-only history is rejected as "
        "non-daily account-live evidence."
    ),
    "required_for_analysis": False,
    "series_id": "Growth at Market Pivot Points [EchoTrade]",
    "status": "DEPRECATED_RETAINED_REJECTED_HISTORY_BY_GOVERNANCE",
    "strategy_version": "echotrade-deprecated-retained-rejected-history:2026-08-29",
}


def test_a_deprecated_retained_history_disposition_is_admitted(tmp_path):
    """The builder has always written this status; the reader must read it."""

    manifest, returns = _history_bundle(tmp_path, [DEPRECATED_DISPOSITION])

    rows, definitions, dispositions, proof = target_series._strategy_history_returns(
        manifest, returns
    )

    assert sorted(rows) == ["Alpha"]
    assert sorted(definitions) == ["Alpha"]
    assert sorted(dispositions) == ["Growth at Market Pivot Points [EchoTrade]"]
    disposition = dispositions["Growth at Market Pivot Points [EchoTrade]"]
    assert disposition["history_status"] == "RETAINED_REJECTED_HISTORY_DEPRECATED"
    assert proof["bundle_id"] == "b" * 64


def test_an_available_history_cannot_hide_inside_a_disposition(tmp_path):
    """The allow-list stays narrow: a captured, admissible status is refused."""

    smuggled = dict(
        DEPRECATED_DISPOSITION,
        history_status="ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
    )
    manifest, returns = _history_bundle(tmp_path, [smuggled])

    with pytest.raises(TargetSeriesError, match="disposition is invalid"):
        target_series._strategy_history_returns(manifest, returns)


def test_a_disposition_for_an_admitted_series_is_refused(tmp_path):
    """A series is either admitted evidence or a declared non-history."""

    conflicting = dict(DEPRECATED_DISPOSITION, series_id="Alpha")
    manifest, returns = _history_bundle(tmp_path, [conflicting])

    with pytest.raises(TargetSeriesError, match="disposition is invalid"):
        target_series._strategy_history_returns(manifest, returns)
'''

assert b'def test_a_deprecated_retained_history_disposition_is_admitted' not in raw
path.write_bytes(raw.rstrip() + nl + test.replace('\n', nl.decode()).encode('utf-8'))
print('ok')
