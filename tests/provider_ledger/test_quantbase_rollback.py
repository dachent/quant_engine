"""Quantbase roll-back: a snapshot taken after the cutoff stands for the cutoff holdings only
when the rows after the cutoff are EXACTLY the declared roll-back set.

Shaped on the real 2026-11-19 / 2026-11-20 case (value-free fixtures): the strategy rebalanced
at the close of the cutoff day (SHV -> SH + GLD, two risky holdings), then again the next day
(SH + GLD -> TQQQ) before the holdings file was captured.  The holdings at the cutoff are the
replay of the fund's filled orders; the replay of ALL orders must reproduce the snapshot, the
opening holdings at the cutoff's official closes must close to the vendor NAV, and nothing
after the cutoff may differ from the declared set.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import provider_ledger as ledger
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from quant_engine.core.contracts import hash_file

RUN = "legacy-20261120-rollback-test"
ACCOUNT = "account-58e75e8d952fa1b8"
CAPTURED_AT = "2026-11-20T21:30:00.000000Z"
D = date(2026, 11, 19)

HOLDINGS = (
    "symbol,quantity,value,latest_price,percentage\nTQQQ,250.00,10030.00,40.12,100.00\n"
)
REBALANCES = (
    "date,symbol,proportion\n"
    "2026-11-18,SHV,1.000000000\n"
    "2026-11-19,SH,0.500000000\n"
    "2026-11-19,GLD,0.500000000\n"
    "2026-11-20,TQQQ,1.000000000\n"
)
NAVS = (
    "time,value,cost_basis,profit_loss\n"
    "11/18/2026,10005.00,10000,0.0005\n"
    "11/19/2026,10010.00,10000,0.001\n"
    "11/20/2026,10030.00,10000,0.003\n"
)
_HEADER = (
    "activity_type,amount,created_at,fund,fund_title,id,time,type,symbol,"
    "filled_at,quantity,executed\n"
)


def _order(row_id: str, day: str, symbol: str, quantity: str, fund: str = "107") -> str:
    stamp = f"{day}T20:54:12Z"
    return f"order,,{stamp},{fund},,{row_id},{stamp},,{symbol},{stamp},{quantity},Y\n"


def _txn(row_id: str, day: str) -> str:
    stamp = f"{day}T20:50:04Z"
    return f"filled_txn,0,{stamp},107,Volatis Strategy,{row_id},{stamp},,RBLNC,,,Y\n"


ACTIVITY = (
    _HEADER
    + _order("o1", "2026-11-18", "SHV", "100")
    + _order("o2", "2026-11-18", "$USD", "10")
    + _txn("t19", "2026-11-19")
    + _order("o3", "2026-11-19", "SHV", "-100")
    + _order("o4", "2026-11-19", "SH", "150")
    + _order("o5", "2026-11-19", "GLD", "20")
    + _order("o6", "2026-11-19", "$USD", "-10")
    + _order("o7", "2026-11-19", "$USD", "10")
    + _txn("t20", "2026-11-20")
    + _order("o8", "2026-11-20", "SH", "-150")
    + _order("o9", "2026-11-20", "GLD", "-20")
    + _order("o10", "2026-11-20", "TQQQ", "250")
    + _order("o11", "2026-11-20", "$USD", "-10")
    # another fund's orders never enter the replay
    + _order("x1", "2026-11-20", "SPY", "5", fund="14")
)
FILES = {
    "csv/dashboard_Volatis_Strategy_holdings.csv": HOLDINGS,
    "csv/volatis_strategy_rebalance_history.csv": REBALANCES,
    "csv/account_activity.csv": ACTIVITY,
    "csv/dashboard_Volatis_Strategy_pricehistory.csv": NAVS,
}
# the activity rows dated after the cutoff and the rebalance row: the declared set
ROLLBACK: dict[str, Any] = {
    "cutoff": "2026-11-19",
    "snapshot_day": "2026-11-20",
    "fund_title": "Volatis Strategy",
    "rebalances": [
        {"date": "2026-11-20", "symbol": "TQQQ", "proportion": "1.000000000"}
    ],
    "activity": [
        {"id": "t20", "activity_type": "filled_txn", "symbol": "RBLNC", "quantity": ""},
        {"id": "o8", "activity_type": "order", "symbol": "SH", "quantity": "-150"},
        {"id": "o9", "activity_type": "order", "symbol": "GLD", "quantity": "-20"},
        {"id": "o10", "activity_type": "order", "symbol": "TQQQ", "quantity": "250"},
        {"id": "o11", "activity_type": "order", "symbol": "$USD", "quantity": "-10"},
        {"id": "x1", "activity_type": "order", "symbol": "SPY", "quantity": "5"},
    ],
}
CLOSES = {
    "SHV": {D: Decimal("100.00")},
    "SH": {D: Decimal("40.05")},
    "GLD": {D: Decimal("200.00")},
}


def _manifest(root: Path, files: dict[str, str]) -> GenerationManifest:
    artifacts = []
    for relative, text in files.items():
        path = root / "source-runs" / RUN / "artifacts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digest, size = hash_file(path)
        artifacts.append(
            SourceArtifact(
                artifact_id=f"source/{relative}",
                sha256=digest,
                size_bytes=size,
                as_of_date=date(2026, 11, 20),
                schema_name="test-csv",
                schema_version="1",
            )
        )
    return GenerationManifest.build(
        as_of_date=date(2026, 11, 20),
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations",),
            observed_start=date(2026, 11, 18),
            observed_end=date(2026, 11, 20),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )


CATALOG = {
    "catalog_schema_version": 1,
    "legacy_lineage": {"run_id": RUN},
    "datasets": [
        {
            "relative_path": "csv/dashboard_Volatis_Strategy_holdings.csv",
            "captured_at": CAPTURED_AT,
            "snapshot_kind": "CAPTURE_TIME_SNAPSHOT",
        }
    ],
}


def _rows(
    tmp_path: Path,
    rollback: object | None = ROLLBACK,
    closes: dict[str, dict[date, Decimal]] | None = CLOSES,
    **edits: str,
) -> list[dict[str, Any]]:
    manifest = _manifest(tmp_path, {**FILES, **edits})
    assignments = {
        ACCOUNT: [
            {
                "effective_start": "2024-10-01",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }
    return ledger._quantbase_positions(
        "gen-1",
        manifest,
        copy.deepcopy(CATALOG),
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        assignments,
        financial_cutoff=D,
        fill_closes=closes,
        rollback=(
            ledger._quantbase_rollback(rollback) if rollback is not None else None
        ),
    )


def test_the_exact_declared_set_rolls_the_snapshot_back_to_the_cutoff(
    tmp_path: Path,
) -> None:
    rows = _rows(tmp_path)
    assert [r["symbol"] for r in rows] == ["GLD", "SH"]
    by = {r["symbol"]: r for r in rows}
    assert by["SH"]["quantity"] == "150" and by["GLD"]["quantity"] == "20"
    for row in rows:
        assert row["as_of_date"] == "2026-11-19"
        assert row["capture_date"] == "2026-11-20"
        assert row["as_of_basis"] == "CAPTURE_TIME_SNAPSHOT_ROLLED_BACK_TO_CUTOFF"
        assert row["value_basis"] == (
            "ROLLED_BACK_ORDER_REPLAY_SCALED_TO_STRATEGY_NAV_AT_CUTOFF"
        )
        closure = row["closure"]
        assert closure["independent_price_check"] == "PASSED"
        assert closure["independent_close_date"] == "2026-11-19"
        assert closure["activity_after_cutoff_count"] == 0
        assert closure["usd_cash"] == "10"
        assert closure["nav_usd"] == "10010.00"
        assert closure["snapshot_difference_usd"] == "0.00"
        evidence = closure["rollback"]
        assert evidence["replay_reproduces_snapshot"] is True
        assert evidence["rollback_activity_count"] == 6
        assert evidence["rollback_rebalance_count"] == 1
        assert Decimal(evidence["opening_residual_to_nav_usd"]) == 0
    # the rows plus the cash equal the vendor NAV exactly; the values are scaled closes
    total = sum(Decimal(r["market_value"]["value"]) for r in rows) + Decimal(10)
    assert total == Decimal("10010.00")
    assert Decimal(by["SH"]["market_value"]["value"]) < Decimal("150") * Decimal(
        "40.05"
    )
    assert closure["rollback"]["value_scale_to_nav"].startswith("0.9992")


def test_without_a_declaration_the_same_files_still_refuse(tmp_path: Path) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="rebalance after the cutoff"):
        _rows(tmp_path, rollback=None)


def test_one_extra_row_after_the_cutoff_refuses(tmp_path: Path) -> None:
    extra = ACTIVITY + _order("z1", "2026-11-20", "SH", "1")
    with pytest.raises(ledger.ProviderLedgerError, match="differs from the declared"):
        _rows(tmp_path, **{"csv/account_activity.csv": extra})


def test_a_missing_declared_row_refuses(tmp_path: Path) -> None:
    kept = ACTIVITY.replace(_order("o9", "2026-11-20", "GLD", "-20"), "")
    with pytest.raises(ledger.ProviderLedgerError, match="differs from the declared"):
        _rows(tmp_path, **{"csv/account_activity.csv": kept})


def test_a_changed_amount_refuses(tmp_path: Path) -> None:
    changed = ACTIVITY.replace(
        _order("o8", "2026-11-20", "SH", "-150"),
        _order("o8", "2026-11-20", "SH", "-149"),
    )
    with pytest.raises(ledger.ProviderLedgerError, match="differs from the declared"):
        _rows(tmp_path, **{"csv/account_activity.csv": changed})


def test_an_undeclared_extra_rebalance_after_the_cutoff_refuses(
    tmp_path: Path,
) -> None:
    more = REBALANCES + "2026-11-20,SH,0.500000000\n"
    with pytest.raises(ledger.ProviderLedgerError, match="rebalance rows after"):
        _rows(tmp_path, **{"csv/volatis_strategy_rebalance_history.csv": more})


def test_a_declared_rebalance_dated_at_the_cutoff_does_not_match(
    tmp_path: Path,
) -> None:
    """A declaration cannot launder a rebalance dated at or before the cutoff: the rows
    after the cutoff must equal the declared rebalances, so a declared 11-19 row differs."""

    declared = copy.deepcopy(ROLLBACK)
    declared["rebalances"] = [
        {"date": "2026-11-19", "symbol": "SH", "proportion": "0.500000000"}
    ]
    with pytest.raises(ledger.ProviderLedgerError, match="rebalance rows after"):
        _rows(tmp_path, rollback=declared)


@pytest.mark.parametrize(
    "field,value",
    [("cutoff", "2026-11-18"), ("snapshot_day", "2026-11-21")],
)
def test_a_declaration_for_another_cutoff_or_snapshot_day_refuses(
    tmp_path: Path, field: str, value: str
) -> None:
    declared = {**ROLLBACK, field: value}
    with pytest.raises(ledger.ProviderLedgerError, match="another cutoff"):
        _rows(tmp_path, rollback=declared)


def test_evidence_that_does_not_reproduce_the_snapshot_refuses(
    tmp_path: Path,
) -> None:
    """The replay of every order must give back the snapshot shares."""

    short = ACTIVITY.replace(
        _order("o10", "2026-11-20", "TQQQ", "250"),
        _order("o10", "2026-11-20", "TQQQ", "240"),
    )
    declared = copy.deepcopy(ROLLBACK)
    declared["activity"][3]["quantity"] = "240"
    with pytest.raises(ledger.ProviderLedgerError, match="replay does not reproduce"):
        _rows(tmp_path, rollback=declared, **{"csv/account_activity.csv": short})


def test_opening_holdings_that_do_not_close_to_the_nav_refuse(tmp_path: Path) -> None:
    bad = {**CLOSES, "SHV": {D: Decimal("90.00")}}
    with pytest.raises(
        ledger.ProviderLedgerError, match="do not close to the strategy"
    ):
        _rows(tmp_path, closes=bad)


@pytest.mark.parametrize("symbol", ["SHV", "SH", "GLD"])
def test_a_missing_official_close_refuses(tmp_path: Path, symbol: str) -> None:
    """The independent close is REQUIRED for a roll-back (never UNAVAILABLE)."""

    bad = {k: v for k, v in CLOSES.items() if k != symbol}
    with pytest.raises(ledger.ProviderLedgerError, match="official close"):
        _rows(tmp_path, closes=bad)
    with pytest.raises(ledger.ProviderLedgerError, match="official close"):
        _rows(tmp_path / "none", closes=None)


def test_a_scale_beyond_the_tolerance_refuses(tmp_path: Path) -> None:
    far = {**CLOSES, "SH": {D: Decimal("50.00")}}
    with pytest.raises(ledger.ProviderLedgerError, match="by more than the tolerance"):
        _rows(tmp_path, closes=far)


def test_the_declaration_is_strict() -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="keys differ"):
        ledger._quantbase_rollback({**ROLLBACK, "extra": 1})
    with pytest.raises(ledger.ProviderLedgerError, match="activity keys differ"):
        ledger._quantbase_rollback(
            {**ROLLBACK, "activity": [{"id": "a", "activity_type": "order"}]}
        )
    with pytest.raises(ledger.ProviderLedgerError, match="declares nothing"):
        ledger._quantbase_rollback({**ROLLBACK, "rebalances": [], "activity": []})
    with pytest.raises(ledger.ProviderLedgerError, match="not a number"):
        ledger._quantbase_rollback(
            {
                **ROLLBACK,
                "activity": [
                    {
                        "id": "a",
                        "activity_type": "order",
                        "symbol": "SH",
                        "quantity": "x",
                    }
                ],
            }
        )


def test_the_snapshot_day_equal_to_the_cutoff_path_is_unchanged(
    tmp_path: Path,
) -> None:
    """Nothing is rolled back when the snapshot day is the cutoff (no declaration needed)."""

    manifest = _manifest(tmp_path, FILES)
    rows = ledger._quantbase_positions(
        "gen-1",
        manifest,
        copy.deepcopy(CATALOG),
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        {
            ACCOUNT: [
                {
                    "effective_start": "2024-10-01",
                    "mapping_status": "MAPPED",
                    "strategy_id": "volatis-strategy",
                }
            ]
        },
        financial_cutoff=None,
    )
    (row,) = rows
    assert row["value_basis"] == "CAPTURE_TIME_SNAPSHOT_VALUE"
    assert row["symbol"] == "TQQQ"


def test_the_spec_key_reaches_the_builder() -> None:
    source = Path(ledger.__file__).read_text(encoding="utf-8")
    assert '"quantbase_rollback",' in source
    assert "rollback=rollback," in source
    assert "rollback=ctx.rollback," in source


def test_a_price_printed_to_the_cent_may_differ_by_its_rounding_not_more(
    tmp_path: Path,
) -> None:
    """Real case: 1,176.08 shares at a printed 41.01 is 48,230.20 against a value of
    48,231.19 (the price is rounded to the cent); a wrong share count still refuses."""

    real = (
        "symbol,quantity,value,latest_price,percentage\n"
        "TQQQ,1176.08,48231.19,41.01,100.00\n"
    )
    manifest = _manifest(
        tmp_path,
        {
            **FILES,
            "csv/dashboard_Volatis_Strategy_holdings.csv": real,
            "csv/dashboard_Volatis_Strategy_pricehistory.csv": NAVS.replace(
                "10030.00", "48231.19"
            ),
        },
    )
    assignments = {
        ACCOUNT: [
            {
                "effective_start": "2024-10-01",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }
    args = (
        "gen-1",
        manifest,
        copy.deepcopy(CATALOG),
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        assignments,
    )
    (row,) = ledger._quantbase_positions(*args, financial_cutoff=None)
    assert row["quantity"] == "1176.08"
    wrong = real.replace("1176.08", "1100.00")
    manifest = _manifest(
        tmp_path / "w",
        {
            **FILES,
            "csv/dashboard_Volatis_Strategy_holdings.csv": wrong,
            "csv/dashboard_Volatis_Strategy_pricehistory.csv": NAVS.replace(
                "10030.00", "48231.19"
            ),
        },
    )
    with pytest.raises(ledger.ProviderLedgerError, match="quantity times price"):
        ledger._quantbase_positions(
            "gen-1",
            manifest,
            copy.deepcopy(CATALOG),
            tmp_path / "w",
            {ACCOUNT},
            {"volatis-strategy"},
            assignments,
            financial_cutoff=None,
        )
