"""Matrixport daily NAV-share positions: admission, closure against the NAV, additions only.

In-repo fixtures carry the real daily_holdings.csv column set with synthetic values (owner
values never live in Git); the opt-in test at the end runs the same builder over the real
retained file named by ``QE_MATRIXPORT_DAILY_HOLDINGS``.
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
    "value_usd_strict_component"
)
PRODUCT = "7361599360326447104"


def _row(day: str, native: str, share: str = "57300.3869283637466547") -> str:
    fields = {
        "currency": "USDT",
        "native_value": native,
        "nav": "0.87",
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


def _manifest(path: Path) -> GenerationManifest:
    digest, size = hash_file(path)
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
        artifacts=(
            SourceArtifact(
                artifact_id="csv/daily_holdings.csv",
                sha256=digest,
                size_bytes=size,
                as_of_date=date(2026, 11, 7),
                schema_name="test-csv",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _setup(
    tmp_path: Path, rows: list[str]
) -> tuple[GenerationManifest, dict[str, Any]]:
    csv_dir = tmp_path / "output" / RUN_ID / "csv"
    csv_dir.mkdir(parents=True)
    path = csv_dir / "daily_holdings.csv"
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8-sig")
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-11-07",
        "source_run_id": RUN_ID,
    }
    return _manifest(path), catalog


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
) -> list[dict[str, Any]]:
    manifest, catalog = _setup(tmp_path, rows)
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
    rows = [_row("2026-11-05", "49891.55"), _row("2026-11-06", "49862.27")]
    positions = _positions(
        tmp_path, rows, _fx({"2026-11-05": "0.999", "2026-11-06": "1.001"})
    )

    assert [p["as_of_date"] for p in positions] == ["2026-11-05", "2026-11-06"]
    first, second = positions
    assert first["quantity"] == "57300.3869283637466547"
    assert first["symbol"] == "USDT" and first["account_id"] == ACCOUNT
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


def test_positions_close_against_the_account_nav_every_day(tmp_path: Path) -> None:
    rows = [_row("2026-11-05", "49891.55"), _row("2026-11-06", "49862.27")]
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


def test_a_position_that_drifts_from_the_nav_beyond_the_tolerance_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("2026-11-06", "49862.27")]
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
        _row("2026-11-04", "0", share="0"),
        _row("2026-11-05", "100"),
        _row("2026-11-06", "100"),
        _row("2026-11-07", "100"),
    ]
    fx = _fx({"2026-11-05": "1", "2026-11-06": "1"})
    positions = _positions(tmp_path, rows, fx, cutoff=date(2026, 11, 6))
    assert [p["as_of_date"] for p in positions] == ["2026-11-05", "2026-11-06"]


def test_without_the_contract_fx_path_there_are_no_positions(tmp_path: Path) -> None:
    assert _positions(tmp_path, [_row("2026-11-06", "10")], None) == []


def test_positions_are_pure_additions_to_the_projected_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("2026-11-05", "49891.55"), _row("2026-11-06", "49862.27")]
    fx = _fx({"2026-11-05": "0.999", "2026-11-06": "1.001"})
    manifest, catalog = _setup(tmp_path, rows)

    def project() -> dict[str, list[dict[str, Any]]]:
        facts, _ = ledger._project_account_facts(  # noqa: SLF001
            "matrixport",
            "gen-1",
            catalog,
            {ACCOUNT},
            STRATEGIES,
            {},
            manifest=manifest,
            root=tmp_path,
            fx_rates=fx,
        )
        return {key: list(value) for key, value in facts.items()}

    monkeypatch.setitem(ledger._ACCOUNT_FACTS, "matrixport", ("nav_observations",))  # noqa: SLF001
    before = project()
    monkeypatch.setitem(
        ledger._ACCOUNT_FACTS,  # noqa: SLF001
        "matrixport",
        ("nav_observations", "positions"),
    )
    after = project()
    assert "positions" not in before
    assert after.pop("positions") and len(after["nav_observations"]) == 2
    assert after == before  # every other fact byte-identical: positions only add


REAL = os.environ.get("QE_MATRIXPORT_DAILY_HOLDINGS")


@pytest.mark.skipif(
    not REAL, reason="QE_MATRIXPORT_DAILY_HOLDINGS names the real file (opt-in)"
)
def test_the_real_daily_holdings_file_closes_against_its_nav_for_every_day(
    tmp_path: Path,
) -> None:
    source = Path(str(REAL))
    csv_dir = tmp_path / "output" / RUN_ID / "csv"
    csv_dir.mkdir(parents=True)
    target = csv_dir / "daily_holdings.csv"
    target.write_bytes(source.read_bytes())
    manifest = _manifest(target)
    days = [
        line.split(",")[14]
        for line in target.read_text(encoding="utf-8-sig").splitlines()[1:]
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
    assert len(positions) >= 300
    assert {p["symbol"] for p in positions} == {"USDT"}
    assert len({p["security_id"] for p in positions}) == 1
