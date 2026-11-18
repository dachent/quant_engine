"""Matrixport daily NAV-share positions: admission, independent closure, typed gaps.

In-repo fixtures carry the real daily_holdings.csv / nav_daily.csv column sets with synthetic
values (owner values never live in Git); the opt-in test at the end runs the same builder over
the real retained run named by ``QE_MATRIXPORT_RUN_CSV_DIR``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import provider_ledger as ledger  # noqa: E402

RUN_ID = "20261109T135114.510Z"
ACCOUNT = "account-d2e5821e608e25f1"
STRATEGIES = {"robuxio-crypto"}
# The real header of csv/daily_holdings.csv (valuation columns included).
HEADER = (
    "before_fee_native_value,before_fee_nav,chicago_date_at_utc_midnight,currency,"
    "fx_available,missing_reason,native_value,native_value_api_reported_current_snapshot,"
    "nav,nav_available,product_id,reconciliation_class,share,source_endpoint,utc_date,"
    "utc_midnight_chicago,utc_midnight_ms,utc_midnight_utc,valuation_source,"
    "value_usd_api_rate,value_usd_api_reported,value_usd_api_reported_current_snapshot,"
    "value_usd_observed,value_usd_parity_assumed,"
    "value_usd_strict_component"
)
NAV_HEADER = (
    "amended_nav,before_fee_nav,day_timestamp,day_timestamp_chicago,day_timestamp_ms,"
    "day_timestamp_utc,is_daily_first,nav,nav_type,product_id"
)
PRODUCT = "7361599360326447104"
SHARE = "100"


def _row(day: str, nav: str, share: str = SHARE, native: str | None = None) -> str:
    fields = {
        "currency": "USDT",
        "native_value": native
        if native is not None
        else format(Decimal(share) * Decimal(nav), "f"),
        "nav": nav,
        "nav_available": "true",
        "product_id": PRODUCT,
        "reconciliation_class": "exact",
        "share": share,
        "source_endpoint": "derived",
        "utc_date": day,
        "valuation_source": "assumed_stablecoin_parity",
        "fx_available": "true",
    }
    return ",".join(fields.get(name, "") for name in HEADER.split(","))


def _nav_row(day: str, nav: str) -> str:
    fields = {
        "day_timestamp_utc": f"{day}T00:00:00+00:00",
        "is_daily_first": "1",
        "nav": nav,
        "nav_type": "normal",
        "product_id": PRODUCT,
    }
    return ",".join(fields.get(name, "") for name in NAV_HEADER.split(","))


def _artifact(path: Path, artifact_id: str) -> SourceArtifact:
    digest, size = hash_file(path)
    return SourceArtifact(
        artifact_id=artifact_id,
        sha256=digest,
        size_bytes=size,
        as_of_date=date(2026, 11, 7),
        schema_name="test-csv",
        schema_version="1",
    )


def _manifest(*paths: tuple[Path, str]) -> GenerationManifest:
    return GenerationManifest.build(
        as_of_date=date(2026, 11, 7),
        adapter=AdapterIdentity(name="test-matrixport-positions", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("daily_holdings",),
            observed_start=date(2026, 11, 7),
            observed_end=date(2026, 11, 7),
        ),
        artifacts=tuple(_artifact(path, artifact_id) for path, artifact_id in paths),
        verification=VerificationStatus.VERIFIED,
    )


def _setup(
    tmp_path: Path,
    rows: list[str],
    nav_rows: list[str] | None = None,
    api_share: str = SHARE,
    event_share: str | None = None,
    boundary: str | None = None,
    api_balance: str | None = None,
) -> tuple[GenerationManifest, dict[str, Any]]:
    csv_dir = tmp_path / "output" / RUN_ID / "csv"
    csv_dir.mkdir(parents=True)
    holdings = csv_dir / "daily_holdings.csv"
    holdings.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8-sig")
    if nav_rows is None:
        nav_rows = [_nav_row(line.split(",")[14], line.split(",")[8]) for line in rows]
    published = csv_dir / "nav_daily.csv"
    published.write_text(
        NAV_HEADER + "\n" + "\n".join(nav_rows) + "\n", encoding="utf-8-sig"
    )
    last = rows[-1].split(",")
    balance = api_balance or format(Decimal(api_share) * Decimal(last[8] or "0"), "f")
    anchors = csv_dir / "holdings.csv"
    name = '[{"language":"en-US","text":"Test Product"}]'.replace('"', '""')
    anchors.write_text(
        "currency,display_name,holding_balance,product_id,shares\n"
        f'USDT,"{name}",{balance},{PRODUCT},{api_share}\n',
        encoding="utf-8-sig",
    )
    reconciliation = csv_dir / "holding_reconciliation.csv"
    reconciliation.write_text(
        "current_share,event_share_sum,product_id,unexplained_opening_share\n"
        f"{api_share},{event_share or api_share},{PRODUCT},0\n",
        encoding="utf-8-sig",
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": boundary or max(line.split(",")[14] for line in rows),
        "source_run_id": RUN_ID,
    }
    return (
        _manifest(
            (holdings, "csv/daily_holdings.csv"),
            (published, "csv/nav_daily.csv"),
            (anchors, "csv/holdings.csv"),
            (reconciliation, "csv/holding_reconciliation.csv"),
        ),
        catalog,
    )


def _span(count: int) -> list[str]:
    """``count`` consecutive days ending on the fixture boundary 2026-11-07."""
    end = date(2026, 11, 7)
    return [date.fromordinal(end.toordinal() - i).isoformat() for i in range(count)][
        ::-1
    ]


def _fx(days: dict[str, str]) -> Any:
    return (
        "f" * 64,
        {
            ("USDT", date.fromisoformat(day)): Decimal(rate)
            for day, rate in days.items()
        },
        (),
    )


def _positions(
    tmp_path: Path,
    rows: list[str],
    fx: Any,
    cutoff: date | None = None,
    nav_rows: list[str] | None = None,
) -> list[dict[str, Any]]:
    manifest, catalog = _setup(tmp_path, rows, nav_rows)
    return ledger._matrixport_positions(  # noqa: SLF001
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {ACCOUNT},
        STRATEGIES,
        {},
        fx_rates=fx,
        financial_cutoff=cutoff,
    )


def test_each_day_yields_one_nav_share_position_valued_with_the_admitted_fx(
    tmp_path: Path,
) -> None:
    rows = [_row("2026-11-05", "498.9155"), _row("2026-11-06", "498.6227")]
    positions = _positions(
        tmp_path, rows, _fx({"2026-11-05": "0.999", "2026-11-06": "1.001"})
    )

    assert [p["as_of_date"] for p in positions] == ["2026-11-05", "2026-11-06"]
    first, second = positions
    assert first["quantity"] == "100" and first["unit_nav"] == "498.9155"
    assert first["symbol"] == "Test Product" and first["account_id"] == ACCOUNT
    assert first["market_value"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "49841.65845",
    }
    assert first["native_value"]["value"] == "49891.55"
    assert second["market_value"]["value"] == "49912.13227"
    assert first["valuation_fx"][0]["rate"] == "0.999"
    assert first["security_id"] == second["security_id"]
    assert first["record_id"] != second["record_id"]
    assert first["value_basis"] == "NAV_SHARE_PRODUCT"
    assert first["closure_status"] == "CONSISTENT_WITH_PUBLISHED_NAV"


def test_positions_close_against_the_account_nav_every_day(tmp_path: Path) -> None:
    rows = [_row("2026-11-05", "498.9155"), _row("2026-11-06", "498.6227")]
    fx = _fx({"2026-11-05": "0.999", "2026-11-06": "1.001"})
    manifest, catalog = _setup(tmp_path, rows)
    navs = ledger._matrixport_nav_observations(  # noqa: SLF001
        "gen-1", manifest, catalog, tmp_path, {ACCOUNT}, STRATEGIES, {}, fx_rates=fx
    )
    positions = ledger._matrixport_positions(  # noqa: SLF001
        "gen-1", manifest, catalog, tmp_path, {ACCOUNT}, STRATEGIES, {}, fx_rates=fx
    )
    by_day = {p["as_of_date"]: Decimal(p["market_value"]["value"]) for p in positions}
    for nav in navs:
        assert by_day[nav["as_of_date"]] == Decimal(nav["nav"]["value"])


def test_a_perturbed_share_count_fails_the_closure(tmp_path: Path) -> None:
    # The NAV is built from native_value; a share count that no longer reproduces it must fail.
    rows = [_row("2026-11-06", "498.6227", share="100", native="49862.27")]
    rows[0] = rows[0].replace(",100,derived", ",101,derived")
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_POSITIONS_(SHARE_ANCHOR|NAV_CLOSURE)",
    ):
        _positions(tmp_path, rows, _fx({"2026-11-06": "1"}))


def test_a_published_unit_nav_that_disagrees_with_the_holdings_fails(
    tmp_path: Path,
) -> None:
    rows = [_row("2026-11-06", "498.6227")]
    nav_rows = [_nav_row("2026-11-06", "498.7")]
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_NAV_CLOSURE"
    ):
        _positions(tmp_path, rows, _fx({"2026-11-06": "1"}), nav_rows=nav_rows)


def test_a_valued_day_without_a_published_unit_nav_is_refused(tmp_path: Path) -> None:
    days = _span(30)
    rows = [_row(day, "10") for day in days]
    nav_rows = [_nav_row(day, "10") for day in days if day != "2026-11-06"]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_POSITIONS_NAV_DAILY_ABSENT.*2026-11-06",
    ):
        _positions(tmp_path, rows, _fx({day: "1" for day in days}), nav_rows=nav_rows)


def test_a_product_id_mismatch_in_nav_daily_is_refused(tmp_path: Path) -> None:
    days = _span(10)
    rows = [_row(day, "10") for day in days]
    nav_rows = [_nav_row(day, "10").replace(PRODUCT, "1") for day in days]
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_NAV_DAILY_ABSENT"
    ):
        _positions(tmp_path, rows, _fx({day: "1" for day in days}), nav_rows=nav_rows)


def test_a_cut_tail_short_of_the_declared_boundary_is_refused(tmp_path: Path) -> None:
    days = _span(10)
    rows = [_row(day, "10") for day in days[:6]]
    nav_rows = [_nav_row(day, "10") for day in days]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_POSITIONS_GAP|SNAPSHOT_DATE_MISMATCH",
    ):
        manifest, catalog = _setup(tmp_path, rows, nav_rows, boundary=days[-1])
        ledger._matrixport_positions(  # noqa: SLF001
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {ACCOUNT},
            STRATEGIES,
            {},
            fx_rates=_fx({day: "1" for day in days}),
        )


def test_a_consistent_nav_forgery_fails_against_the_api_holding_value(
    tmp_path: Path,
) -> None:
    # nav doubled in both files and native_value doubled: per-day checks and shares agree, only
    # the API-reported holding value (another endpoint) disagrees.
    days = _span(3)
    rows = [_row(day, "20") for day in days]
    manifest, catalog = _setup(tmp_path, rows, api_balance="1000")
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_API_VALUE_ANCHOR"
    ):
        ledger._matrixport_positions(  # noqa: SLF001
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {ACCOUNT},
            STRATEGIES,
            {},
            fx_rates=_fx({day: "1" for day in days}),
        )


def test_too_many_native_absent_days_are_refused(tmp_path: Path) -> None:
    days = _span(10)
    rows = [_row(day, "10", native="" if day in days[:3] else None) for day in days]
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_TOO_MANY_GAPS"
    ):
        _positions(tmp_path, rows, _fx({day: "1" for day in days}))


def test_a_consistent_share_forgery_fails_against_the_api_share(tmp_path: Path) -> None:
    # Share and native_value doubled together: every per-day check is self-consistent, only the
    # API-reported holding (a different endpoint) can catch it.
    days = _span(3)
    rows = [_row(day, "10", share="200") for day in days]
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_SHARE_ANCHOR"
    ):
        _positions(tmp_path, rows, _fx({day: "1" for day in days}))


def test_unreconciled_event_shares_are_refused(tmp_path: Path) -> None:
    days = _span(3)
    rows = [_row(day, "10") for day in days]
    manifest, catalog = _setup(tmp_path, rows, event_share="90")
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_SHARE_ANCHOR"
    ):
        ledger._matrixport_positions(  # noqa: SLF001
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {ACCOUNT},
            STRATEGIES,
            {},
            fx_rates=_fx({day: "1" for day in days}),
        )


def test_a_position_that_drifts_from_the_nav_beyond_the_tolerance_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("2026-11-06", "498.6227")]
    fx = _fx({"2026-11-06": "1"})
    manifest, catalog = _setup(tmp_path, rows)
    real = ledger._matrixport_nav_observations  # noqa: SLF001

    def shifted(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        out = real(*args, **kwargs)
        for observation in out:
            value = Decimal(observation["nav"]["value"]) + Decimal("0.01")
            observation["nav"] = {**observation["nav"], "value": format(value, "f")}
        return out

    monkeypatch.setattr(ledger, "_matrixport_nav_observations", shifted)
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_NAV_CLOSURE"
    ):
        ledger._matrixport_positions(  # noqa: SLF001
            "gen-1", manifest, catalog, tmp_path, {ACCOUNT}, STRATEGIES, {}, fx_rates=fx
        )

    def within(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        out = real(*args, **kwargs)
        for observation in out:
            value = Decimal(observation["nav"]["value"]) + Decimal("0.004")
            observation["nav"] = {**observation["nav"], "value": format(value, "f")}
        return out

    monkeypatch.setattr(ledger, "_matrixport_nav_observations", within)
    assert (
        len(
            ledger._matrixport_positions(  # noqa: SLF001
                "gen-1",
                manifest,
                catalog,
                tmp_path,
                {ACCOUNT},
                STRATEGIES,
                {},
                fx_rates=fx,
            )
        )
        == 1
    )


def test_a_nav_day_without_position_rows_is_refused_with_the_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("2026-11-06", "498.6227")]
    manifest, catalog = _setup(tmp_path, rows)
    catalog["observed_boundary_date"] = "2026-11-06"
    real = ledger._matrixport_nav_observations  # noqa: SLF001

    def extra_day(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        out = real(*args, **kwargs)
        return [*out, {**out[0], "as_of_date": "2026-11-07"}]

    monkeypatch.setattr(ledger, "_matrixport_nav_observations", extra_day)
    with pytest.raises(ledger.ProviderLedgerError, match="2026-11-07"):
        ledger._matrixport_positions(  # noqa: SLF001
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {ACCOUNT},
            STRATEGIES,
            {},
            fx_rates=_fx({"2026-11-06": "1", "2026-11-07": "1"}),
        )


def test_days_cut_out_of_the_middle_are_refused_with_the_dates(tmp_path: Path) -> None:
    days = ["2026-11-04", "2026-11-05", "2026-11-06"]
    rows = [_row(day, "10") for day in (days[0], days[2])]
    nav_rows = [_nav_row(day, "10") for day in days]
    fx = _fx({day: "1" for day in days})
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_GAP.*2026-11-05"
    ):
        _positions(tmp_path, rows, fx, nav_rows=nav_rows)


def test_an_active_day_without_a_native_value_is_typed_unavailable(
    tmp_path: Path,
) -> None:
    days = _span(30)
    rows = [_row(day, "10", native="" if day == "2026-11-06" else None) for day in days]
    fx = _fx({day: "1" for day in days})
    positions = _positions(tmp_path, rows, fx)
    by_day = {p["as_of_date"]: p for p in positions}
    gap = by_day["2026-11-06"]
    assert gap["closure_status"] == "NATIVE_VALUE_ABSENT"
    assert gap["market_value"] == {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "value": None,
    }
    assert gap["quantity"] == "100"
    assert by_day["2026-11-05"]["closure_status"] == "CONSISTENT_WITH_PUBLISHED_NAV"


def test_a_duplicate_product_day_is_refused(tmp_path: Path) -> None:
    rows = [_row("2026-11-06", "10"), _row("2026-11-06", "10")]
    with pytest.raises(
        ledger.ProviderLedgerError, match="MATRIXPORT_POSITIONS_DUPLICATE"
    ):
        _positions(tmp_path, rows, _fx({"2026-11-06": "1"}))


def test_zero_share_rows_the_cutoff_and_the_fx_boundary_bound_the_positions(
    tmp_path: Path,
) -> None:
    rows = [
        _row("2026-11-04", "1", share="0", native="0"),
        _row("2026-11-05", "1"),
        _row("2026-11-06", "1"),
        _row("2026-11-07", "1"),
    ]
    fx = _fx({"2026-11-05": "1", "2026-11-06": "1"})
    positions = _positions(tmp_path, rows, fx, cutoff=date(2026, 11, 6))
    assert [p["as_of_date"] for p in positions] == ["2026-11-05", "2026-11-06"]


def test_without_the_contract_fx_path_there_are_no_positions(tmp_path: Path) -> None:
    assert _positions(tmp_path, [_row("2026-11-06", "10")], None) == []


REAL = os.environ.get("QE_MATRIXPORT_RUN_CSV_DIR")


@pytest.mark.skipif(
    not REAL,
    reason="QE_MATRIXPORT_RUN_CSV_DIR names a real run's csv directory (opt-in)",
)
def test_the_real_run_is_consistent_and_types_its_provider_gaps(tmp_path: Path) -> None:
    source = Path(str(REAL))
    csv_dir = tmp_path / "output" / RUN_ID / "csv"
    csv_dir.mkdir(parents=True)
    paths = []
    for name in (
        "daily_holdings.csv",
        "nav_daily.csv",
        "holdings.csv",
        "holding_reconciliation.csv",
    ):
        target = csv_dir / name
        target.write_bytes((source / name).read_bytes())
        paths.append((target, f"csv/{name}"))
    manifest = _manifest(*paths)
    days = [
        line.split(",")[14]
        for line in paths[0][0].read_text(encoding="utf-8-sig").splitlines()[1:]
        if line.strip()
    ]
    fx: Any = (
        "f" * 64,
        {("USDT", date.fromisoformat(day)): Decimal("1") for day in days},
        (),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": max(days),
        "source_run_id": RUN_ID,
    }
    positions = ledger._matrixport_positions(  # noqa: SLF001
        "gen-real", manifest, catalog, tmp_path, {ACCOUNT}, STRATEGIES, {}, fx_rates=fx
    )
    assert len(positions) >= 250
    assert {p["symbol"] for p in positions} == {"High Sharpe High Vol"}
    assert len({p["security_id"] for p in positions}) == 1
    assert {p["closure_status"] for p in positions} <= {
        "CONSISTENT_WITH_PUBLISHED_NAV",
        "NATIVE_VALUE_ABSENT",
    }
