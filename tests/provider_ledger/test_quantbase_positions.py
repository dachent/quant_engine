"""Quantbase positions: the capture-time holdings snapshot stands for the cutoff holdings.

Shaped on the real files (value-free fixtures): a cash line and one risky holding, the
rebalance history ending before the cutoff, no account activity after it, a strategy NAV
series the snapshot sums to, and a generation whose as-of date is EARLIER than the moment
its holdings file was captured (the real generations differ this way).
"""

from __future__ import annotations

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

RUN = "legacy-20261109-test"
ACCOUNT = "account-58e75e8d952fa1b8"
CAPTURED_AT = "2026-11-09T16:03:38.852591Z"
FILES = {
    "csv/dashboard_Volatis_Strategy_holdings.csv": (
        "symbol,quantity,value,latest_price,percentage\n"
        "$USD,20.00,20.00,1.00,0.04\n"
        "SHV,436.19,48041.98,110.14,99.96\n"
    ),
    "csv/volatis_strategy_rebalance_history.csv": (
        "date,symbol,proportion\n"
        "2026-10-29,SHV,1.000000000\n"
        "2026-11-04,TQQQ,1.000000000\n"
        "2026-11-05,SHV,1.000000000\n"
    ),
    "csv/account_activity.csv": (
        "activity_type,amount,created_at,fund,id,time,type,symbol,filled_at\n"
        "order,10,2026-11-05T15:00:00Z,f,1,2026-11-05T15:00:00Z,buy,SHV,2026-11-05T15:00:00Z\n"
        "dividend,203.40,2026-11-05T15:00:00Z,f,2,2026-11-05T15:00:00Z,dividend,SHV,\n"
        "journal,169.86,2026-11-06T15:00:00Z,f,3,2026-11-06T15:00:00Z,journal,,\n"
    ),
    "csv/dashboard_Volatis_Strategy_pricehistory.csv": (
        "time,value,cost_basis,profit_loss\n"
        "11/05/2026,48050.00,46000,0.01\n"
        "11/06/2026,48057.62,46000,0.01\n"
        "11/09/2026,48061.98,46652.14,0.0302\n"
    ),
}


def _manifest(
    root: Path, files: dict[str, str], as_of: date = date(2026, 11, 6)
) -> GenerationManifest:
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
                as_of_date=as_of,
                schema_name="test-csv",
                schema_version="1",
            )
        )
    return GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations",),
            observed_start=date(2026, 11, 5),
            observed_end=as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )


def _catalog(
    captured_at: str | None = CAPTURED_AT, kind: str = "CAPTURE_TIME_SNAPSHOT"
) -> dict[str, Any]:
    datasets = []
    if captured_at is not None:
        datasets.append(
            {
                "relative_path": "csv/dashboard_Volatis_Strategy_holdings.csv",
                "captured_at": captured_at,
                "snapshot_kind": kind,
            }
        )
    return {
        "catalog_schema_version": 1,
        "legacy_lineage": {"run_id": RUN},
        "datasets": datasets,
    }


def _rows(
    tmp_path: Path,
    cutoff: date | None,
    catalog: dict[str, Any] | None = None,
    **edits: str,
) -> list[dict[str, Any]]:
    files = {**FILES, **edits}
    manifest = _manifest(tmp_path, files)
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
        catalog if catalog is not None else _catalog(),
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        assignments,
        financial_cutoff=cutoff,
    )


def test_the_capture_day_not_the_generation_as_of_is_the_snapshot_day(
    tmp_path: Path,
) -> None:
    """KILL (review F1): the generation says 2026-11-06, the file was captured 11-09."""

    (row,) = _rows(tmp_path, date(2026, 11, 6))
    assert row["capture_date"] == "2026-11-09"
    assert row["closure"]["snapshot_day"] == "2026-11-09"
    assert row["as_of_date"] == "2026-11-06"
    assert row["value_basis"] == "DERIVED_FROM_STRATEGY_NAV_AT_CUTOFF"
    assert row["market_value"]["value"] == "48037.62"
    assert row["closure"]["snapshot_total_usd"] == row["closure"][
        "snapshot_nav_usd"
    ] or (row["closure"]["snapshot_difference_usd"] == "0.00")
    assert row["closure"]["snapshot_difference_usd"] == "0.00"
    assert row["closure"]["usd_cash"] == "20.00"
    assert row["mark_price"] == row["snapshot_price"] == "110.14"
    assert row["closure"]["activity_after_cutoff_count"] == 0
    assert row["closure"]["last_activity_date"] == "2026-11-06"
    assert row["strategy_id"] == "volatis-strategy"
    assert isinstance(row["quantity"], str) and row["quantity"] == "436.19"
    assert row["security_id"].startswith("security-quantbase-")


def test_a_cutoff_at_the_snapshot_day_uses_the_snapshot_value(tmp_path: Path) -> None:
    (row,) = _rows(tmp_path, None)
    assert row["value_basis"] == "CAPTURE_TIME_SNAPSHOT_VALUE"
    assert row["market_value"]["value"] == "48041.98"
    assert row["as_of_date"] == "2026-11-09"


@pytest.mark.parametrize(
    "line",
    [
        "order,10,2026-11-07T15:00:00Z,f,9,2026-11-07T15:00:00Z,buy,SHV,2026-11-07T15:00:00Z\n",
        "dividend,5.00,2026-11-08T15:00:00Z,f,9,2026-11-08T15:00:00Z,dividend,SHV,\n",
        "journal,5.00,2026-11-09T11:00:00Z,f,9,2026-11-09T11:00:00Z,journal,,\n",
        "fee,5.00,2026-11-09T11:00:00Z,f,9,2026-11-09T11:00:00Z,aum_fee,,\n",
    ],
)
def test_any_account_activity_after_the_cutoff_refuses(
    tmp_path: Path, line: str
) -> None:
    """KILL (review F3): a dividend, journal or fee moves cash as an order moves shares."""

    edit = FILES["csv/account_activity.csv"] + line
    with pytest.raises(ledger.ProviderLedgerError, match="activity after the cutoff"):
        _rows(tmp_path, date(2026, 11, 6), **{"csv/account_activity.csv": edit})


def test_a_rebalance_after_the_cutoff_refuses(tmp_path: Path) -> None:
    edit = FILES["csv/volatis_strategy_rebalance_history.csv"] + (
        "2026-11-08,TQQQ,1.000000000\n"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="rebalance after the cutoff"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/volatis_strategy_rebalance_history.csv": edit},
        )


def _rows_with_closes(
    tmp_path: Path, closes: dict[str, dict[date, Decimal]], **edits: str
) -> list[dict[str, Any]]:
    files = {**FILES, **edits}
    manifest = _manifest(tmp_path, files)
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
        _catalog(),
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        assignments,
        financial_cutoff=date(2026, 11, 6),
        fill_closes=closes,
    )


def test_a_wrong_share_count_fails_the_independent_close_check(
    tmp_path: Path,
) -> None:
    """KILL (review F2): 400 shares with an internally consistent file is 120 per share;
    the official close of the cutoff is 110.13, so the independent check refuses."""

    consistent_wrong = (
        "symbol,quantity,value,latest_price,percentage\n"
        "$USD,20.00,20.00,1.00,0.04\n"
        "SHV,400.00,48041.98,120.10495,99.96\n"
    )
    closes = {"SHV": {date(2026, 11, 6): Decimal("110.13")}}
    with pytest.raises(ledger.ProviderLedgerError, match="official close"):
        _rows_with_closes(
            tmp_path,
            closes,
            **{"csv/dashboard_Volatis_Strategy_holdings.csv": consistent_wrong},
        )


def test_the_independent_close_passes_for_the_right_count_and_is_named_when_absent(
    tmp_path: Path,
) -> None:
    closes = {"SHV": {date(2026, 11, 6): Decimal("110.13")}}
    (row,) = _rows_with_closes(tmp_path / "a", closes)
    assert row["closure"]["independent_price_check"] == "PASSED"
    assert row["closure"]["independent_close"] == "110.13"
    (bare,) = _rows(tmp_path / "b", date(2026, 11, 6))
    assert bare["closure"]["independent_price_check"] == "UNAVAILABLE"
    assert "independent_close" not in bare["closure"]


def test_a_pre_fill_nav_that_disagrees_with_the_holdings_refuses(
    tmp_path: Path,
) -> None:
    """KILL (review F2, real case): a cutoff NAV that implies a price 2 percent off."""

    nav = FILES["csv/dashboard_Volatis_Strategy_pricehistory.csv"].replace(
        "11/06/2026,48057.62", "11/06/2026,47134.92"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="implies a price"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/dashboard_Volatis_Strategy_pricehistory.csv": nav},
        )


def test_holdings_that_do_not_close_to_the_cent_refuse(tmp_path: Path) -> None:
    """KILL (review F1): the tolerance is a cent, not half a percent."""

    edit = FILES["csv/dashboard_Volatis_Strategy_holdings.csv"].replace(
        "48041.98", "48042.10"
    )
    with pytest.raises(ledger.ProviderLedgerError):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/dashboard_Volatis_Strategy_holdings.csv": edit},
        )


def test_quantity_times_price_must_equal_the_value(tmp_path: Path) -> None:
    edit = FILES["csv/dashboard_Volatis_Strategy_holdings.csv"].replace(
        "110.14,99.96", "109.00,99.96"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="quantity times price"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/dashboard_Volatis_Strategy_holdings.csv": edit},
        )


def test_two_risky_holdings_cannot_be_valued_at_an_earlier_cutoff(
    tmp_path: Path,
) -> None:
    edit = (
        "symbol,quantity,value,latest_price,percentage\n"
        "$USD,20.00,20.00,1.00,0.04\n"
        "SHV,200.00,22000.00,110.00,45.8\n"
        "TQQQ,300.00,26041.98,86.8066,54.2\n"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="cannot be valued"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/dashboard_Volatis_Strategy_holdings.csv": edit},
        )


def test_a_missing_cutoff_nav_refuses(tmp_path: Path) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="cannot be valued"):
        _rows(tmp_path, date(2026, 11, 7))


@pytest.mark.parametrize(
    "catalog,fragment",
    [
        (_catalog(captured_at=None), "capture time is absent"),
        (_catalog(kind="DAILY_SERIES"), "not a capture-time snapshot"),
    ],
)
def test_the_capture_time_must_come_from_the_holdings_dataset(
    tmp_path: Path, catalog: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match=fragment):
        _rows(tmp_path, date(2026, 11, 6), catalog=catalog)


def test_a_capture_without_a_holdings_file_has_no_positions_not_an_error(
    tmp_path: Path,
) -> None:
    files = {k: v for k, v in FILES.items() if "holdings" not in k}
    manifest = _manifest(tmp_path, files)
    rows = ledger._quantbase_positions(
        "gen-1",
        manifest,
        _catalog(),
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        {},
        financial_cutoff=date(2026, 11, 6),
    )
    assert rows == []


def test_the_builder_is_not_yet_registered_as_a_declared_fact() -> None:
    """Registering the fact needs the financial contract's target facts to name it
    (carrier-7 pin): the ledger integration owner does both in one change."""

    assert "positions" not in ledger._ACCOUNT_FACTS["quantbase"]
    assert callable(ledger._csv_quantbase_positions)


def test_the_position_close_spec_key_is_pinned_and_never_restates_a_nav_day(
    tmp_path: Path,
) -> None:
    """The governed SHV close pin (spec key quantbase_position_closes): hash- and
    size-verified, accepted as an optional ledger spec key, and parsed apart from
    ``quantbase_fill_closes`` so it can never switch on the fill-day NAV restatement."""

    import hashlib

    csv_path = tmp_path / "SHV.csv"
    csv_path.write_text(
        "date,close,adjclose\n2026-11-06,110.13,110.13\n", encoding="utf-8"
    )
    raw = csv_path.read_bytes()
    ref = {
        "symbol": "SHV",
        "path": str(csv_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    closes = ledger._quantbase_fill_closes([ref], "quantbase_position_closes")
    assert closes == {"SHV": {date(2026, 11, 6): Decimal("110.13")}}
    with pytest.raises(ledger.ProviderLedgerError, match="quantbase_position_closes"):
        ledger._quantbase_fill_closes(
            [{**ref, "sha256": "0" * 64}], "quantbase_position_closes"
        )
    # the closes reach the position builder, and only it
    (row,) = _rows_with_closes(tmp_path / "w", closes)
    assert row["closure"]["independent_price_check"] == "PASSED"
    source = Path(ledger.__file__).read_text(encoding="utf-8")
    assert "position_closes=position_closes" in source
