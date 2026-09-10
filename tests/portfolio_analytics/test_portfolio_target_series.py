from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_target_series.py"
SPEC = importlib.util.spec_from_file_location("portfolio_target_series", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio target-series builder")
target_series = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = target_series
SPEC.loader.exec_module(target_series)
TargetSeriesError = target_series.TargetSeriesError
build_matrixport_targets = target_series.build_matrixport_targets
build_matrixport_strategy_evidence = target_series.build_matrixport_strategy_evidence
build_decomposition_inputs = target_series.build_decomposition_inputs
build_quantbase_targets = target_series.build_quantbase_targets
build_portfolio123_evidence = target_series.build_portfolio123_evidence
build_strategy_evidence = target_series.build_strategy_evidence
build_layered_correlation_inputs = target_series.build_layered_correlation_inputs
build_provider_ledger_targets = target_series.build_provider_ledger_targets


def _write(path: Path, content: str) -> tuple[str, int]:
    data = content.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(data)


def _sources(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    base = tmp_path / "base.csv"
    _write(
        base,
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "Existing,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n",
    )
    nav = tmp_path / "nav.csv"
    nav_hash, nav_size = _write(
        nav,
        "product_id,timestamp_utc,nav\n"
        "P1,2026-01-01T00:00:00+00:00,1\n"
        "P1,2026-01-02T00:00:00+00:00,1.1\n"
        "P1,2026-01-03T00:00:00+00:00,0.99\n"
        "OTHER,2026-01-03T00:00:00+00:00,8\n",
    )
    holdings = tmp_path / "holdings.csv"
    holding_hash, holding_size = _write(holdings, "product_id,shares\nP1,100\n")
    subscriptions = tmp_path / "subscriptions.csv"
    subscription_hash, subscription_size = _write(
        subscriptions,
        "confirm_nav_time_utc,product_id,status,subscription_share,subscription_time_utc\n"
        "2026-01-02T00:00:00+00:00,P1,300,100,2026-01-01T21:00:00+00:00\n",
    )
    manifest = tmp_path / "generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "a" * 64,
                "verification": "VERIFIED",
                "coverage": {
                    "declared_exclusions": [],
                    "known_gaps": [],
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
                "artifacts": [
                    {
                        "artifact_id": "csv/nav_daily.csv",
                        "sha256": nav_hash,
                        "size_bytes": nav_size,
                    },
                    {
                        "artifact_id": "csv/holdings.csv",
                        "sha256": holding_hash,
                        "size_bytes": holding_size,
                    },
                    {
                        "artifact_id": "csv/subscription_details.csv",
                        "sha256": subscription_hash,
                        "size_bytes": subscription_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return base, manifest, nav, holdings, subscriptions


def _matrixport_exclusions(*dates: str) -> list[dict[str, str]]:
    return [
        {
            "item": f"{kind}:{observed}",
            "provenance": "owner-accepted 2026-08-14, issue #287",
            "reason": "provider NAV endpoint returned no data for this date",
        }
        for kind in (
            "fx_missing_date",
            "nav_missing_date",
            "reconciliation_incomplete_date",
        )
        for observed in dates
    ]


def _rebind_matrixport_nav(
    manifest: Path, nav: Path, *, declared_dates: tuple[str, ...]
) -> None:
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    nav_artifact = next(
        row
        for row in manifest_value["artifacts"]
        if row["artifact_id"] == "csv/nav_daily.csv"
    )
    nav_artifact["sha256"] = hashlib.sha256(nav.read_bytes()).hexdigest()
    nav_artifact["size_bytes"] = nav.stat().st_size
    manifest_value["coverage"]["declared_exclusions"] = _matrixport_exclusions(
        *declared_dates
    )
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")


def _rebind_matrixport_daily_holdings(
    manifest: Path,
    nav: Path,
    *,
    declared_dates: tuple[str, ...],
) -> Path:
    """Bind the exact per-date share evidence used by a declared span."""

    daily = nav.with_name("daily_holdings.csv")
    nav_rows = list(csv.DictReader(io.StringIO(nav.read_text(encoding="utf-8"))))
    rows = ["product_id,utc_date,share,nav,nav_available\n"]
    for row in nav_rows:
        if row["product_id"] != "P1":
            continue
        observed = target_series._utc_day(row["timestamp_utc"])
        rows.append(f"P1,{observed.isoformat()},100,{row['nav']},true\n")
    for observed in declared_dates:
        rows.append(f"P1,{observed},100,,false\n")
    daily_hash, daily_size = _write(daily, "".join(rows))
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = next(
        (
            row
            for row in manifest_value["artifacts"]
            if row["artifact_id"] == "csv/daily_holdings.csv"
        ),
        None,
    )
    if artifact is None:
        manifest_value["artifacts"].append(
            {
                "artifact_id": "csv/daily_holdings.csv",
                "sha256": daily_hash,
                "size_bytes": daily_size,
            }
        )
    else:
        artifact["sha256"] = daily_hash
        artifact["size_bytes"] = daily_size
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    return daily


def test_matrixport_target_starts_account_live_only_after_funding(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path)
    first, proof_bytes = build_matrixport_targets(*sources, "P1", "Robuxio")
    second, second_proof = build_matrixport_targets(*sources, "P1", "Robuxio")
    assert first == second
    assert proof_bytes == second_proof
    rows = list(csv.DictReader(io.StringIO(first.decode("utf-8"))))
    robuxio = [row for row in rows if row["series_id"] == "Robuxio"]
    assert [row["date"] for row in robuxio] == ["2026-01-03"]
    assert [row["return"] for row in robuxio] == ["-0.1"]
    assert {row["evidence_layer"] for row in robuxio} == {"ACCOUNT_LIVE"}
    proof = json.loads(proof_bytes)
    assert proof["builder"] == "portfolio-target-series/matrixport-4"
    assert proof["coverage"]["nav_observations"] == 3
    assert proof["coverage"]["provider_live_observed_end"] == "2026-01-01"
    assert proof["coverage"]["account_live_start"] == "2026-01-02"
    assert proof["coverage"]["funding_event_date"] == "2026-01-02"
    assert proof["coverage"]["subscription_initiated_date"] == "2026-01-01"
    assert proof["coverage"]["boundary_return_excluded"] is True
    assert proof["coverage"]["return_observations"] == 1
    assert proof["coverage"]["source_gap_policy"] == {
        "authority_bound": False,
        "declared_missing_source_dates": [],
        "omitted_return_dates": [],
        "span_return_receipts": [],
        "unavailable_return_dates": [],
    }


def test_matrixport_target_omits_declared_gaps_and_spanning_returns(
    tmp_path: Path,
) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    _write(
        nav,
        "product_id,timestamp_utc,nav\n"
        "P1,2026-01-01T00:00:00Z,1\n"
        "P1,2026-01-02T00:00:00Z,1.1\n"
        "P1,2026-01-04T00:00:00Z,1.2\n"
        "P1,2026-01-05T00:00:00Z,1.32\n"
        "P1,2026-01-07T00:00:00Z,1.4\n"
        "P1,2026-01-08T00:00:00Z,1.54\n",
    )
    _rebind_matrixport_nav(manifest, nav, declared_dates=("2026-01-03", "2026-01-06"))
    daily_holdings = _rebind_matrixport_daily_holdings(
        manifest, nav, declared_dates=("2026-01-03", "2026-01-06")
    )

    targets, proof_bytes = build_matrixport_targets(
        base,
        manifest,
        nav,
        holdings,
        subscriptions,
        "P1",
        "Robuxio",
        daily_holdings_path=daily_holdings,
    )
    rows = list(csv.DictReader(io.StringIO(targets.decode("utf-8"))))
    robuxio = [row for row in rows if row["series_id"] == "Robuxio"]
    assert [(row["date"], row["return"]) for row in robuxio] == [
        ("2026-01-05", "0.1"),
        ("2026-01-08", "0.1"),
    ]
    policy = json.loads(proof_bytes)["coverage"]["source_gap_policy"]
    assert policy["declared_missing_source_dates"] == [
        "2026-01-03",
        "2026-01-06",
    ]
    assert policy["omitted_return_dates"] == ["2026-01-04", "2026-01-07"]
    assert policy["unavailable_return_dates"] == [
        "2026-01-03",
        "2026-01-04",
        "2026-01-06",
        "2026-01-07",
    ]
    assert policy["authority_bound"] is False


def test_matrixport_target_retains_declared_gap_span_for_epoch_twr_only(
    tmp_path: Path,
) -> None:
    """A missing daily point cannot erase a verified multi-day owner return."""

    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    subscription_hash, subscription_size = _write(
        subscriptions,
        "confirm_nav_time_utc,product_id,status,subscription_share,subscription_time_utc\n"
        "2025-12-06T00:00:00Z,P1,300,100,2025-12-05T21:00:00Z\n",
    )
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    subscription_artifact = next(
        row
        for row in manifest_value["artifacts"]
        if row["artifact_id"] == "csv/subscription_details.csv"
    )
    subscription_artifact["sha256"] = subscription_hash
    subscription_artifact["size_bytes"] = subscription_size
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    owner_start = date(2025, 12, 6)
    cutoff = date(2026, 8, 13)
    jan_start = date(2026, 1, 26)
    jan_end = date(2026, 1, 28)
    jul_start = date(2026, 7, 2)
    jul_end = date(2026, 7, 4)
    with localcontext() as context:
        context.prec = 60
        jan_factor = Decimal("0.8315377590244603") / Decimal("0.8460189019350154")
        jul_factor = Decimal("0.855405567489686") / Decimal("0.8724061827323055")
        starting_nav = Decimal("49891.54999999999999999922202")
        terminal_nav = Decimal("49273.51697711999539954620532")
        old_daily_only_factor = terminal_nav / starting_nav / jan_factor / jul_factor
        values = {
            owner_start: starting_nav,
            owner_start + timedelta(days=1): starting_nav * old_daily_only_factor,
            jan_start: starting_nav * old_daily_only_factor,
            jan_end: starting_nav * old_daily_only_factor * jan_factor,
            jul_start: starting_nav * old_daily_only_factor * jan_factor,
            jul_end: starting_nav * old_daily_only_factor * jan_factor * jul_factor,
            cutoff: terminal_nav,
        }
    missing = {date(2026, 1, 27), date(2026, 7, 3)}
    rows = ["product_id,timestamp_utc,nav\n"]
    observed = owner_start
    carried = starting_nav
    while observed <= cutoff:
        if observed not in missing:
            carried = values.get(observed, carried)
            rows.append(f"P1,{observed.isoformat()}T00:00:00Z,{carried}\n")
        observed += timedelta(days=1)
    rows.append(f"P1,2026-08-14T00:00:00Z,{terminal_nav * Decimal(2)}\n")
    _write(nav, "".join(rows))
    _rebind_matrixport_nav(manifest, nav, declared_dates=("2026-01-27", "2026-07-03"))
    daily_holdings = _rebind_matrixport_daily_holdings(
        manifest, nav, declared_dates=("2026-01-27", "2026-07-03")
    )

    _targets, proof_bytes = build_matrixport_targets(
        base,
        manifest,
        nav,
        holdings,
        subscriptions,
        "P1",
        "Robuxio",
        admission_cutoff=cutoff,
        daily_holdings_path=daily_holdings,
    )
    coverage = json.loads(proof_bytes)["coverage"]

    assert Decimal(coverage["daily_cumulative_return"]).quantize(
        Decimal("1e-24")
    ) == Decimal("0.02478159992980962757481709200").quantize(Decimal("1e-24"))
    assert Decimal(coverage["epoch_cumulative_return"]).quantize(
        Decimal("1e-24")
    ) == Decimal("-0.01238752900801848410107576010").quantize(Decimal("1e-24"))
    assert coverage["epoch_cumulative_return"] != coverage["daily_cumulative_return"]
    assert coverage["account_nav_observations"] == 249
    assert coverage["admission_cutoff"] == "2026-08-13"
    assert coverage["return_observations"] == 246
    assert coverage["epoch_holding_period_link_observations"] == 248
    receipt = coverage["epoch_hpr_receipt"]
    assert receipt["daily_return_observations"] == 246
    assert receipt["epoch_holding_period_link_observations"] == 248
    assert receipt["admission_cutoff"] == "2026-08-13"
    assert receipt["epoch_cumulative_return"] == ("-0.0123875290080184841010757599")
    assert receipt["daily_cumulative_return"] == coverage["daily_cumulative_return"]
    assert receipt["epoch_cumulative_return"] != receipt["daily_cumulative_return"]
    assert len(receipt["daily_return_series_sha256"]) == 64
    assert [
        (
            row["missing_source_date"],
            row["span_start_date"],
            row["span_end_date"],
            row["span_days"],
        )
        for row in coverage["span_return_receipts"]
    ] == [
        ("2026-01-27", "2026-01-26", "2026-01-28", 2),
        ("2026-07-03", "2026-07-02", "2026-07-04", 2),
    ]


def test_matrixport_target_rejects_gap_span_when_retained_endpoint_shares_change(
    tmp_path: Path,
) -> None:
    """A current holding cannot stand in for historical endpoint shares."""

    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    _write(
        nav,
        "product_id,timestamp_utc,nav\n"
        "P1,2026-01-01T00:00:00Z,1\n"
        "P1,2026-01-02T00:00:00Z,1.1\n"
        "P1,2026-01-04T00:00:00Z,1.2\n",
    )
    _rebind_matrixport_nav(manifest, nav, declared_dates=("2026-01-03",))
    daily_holdings = tmp_path / "daily_holdings.csv"
    daily_hash, daily_size = _write(
        daily_holdings,
        "product_id,utc_date,share,nav,nav_available\n"
        "P1,2026-01-02,100,1.1,true\n"
        "P1,2026-01-03,100,,false\n"
        "P1,2026-01-04,99,1.2,true\n",
    )
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["artifacts"].append(
        {
            "artifact_id": "csv/daily_holdings.csv",
            "sha256": daily_hash,
            "size_bytes": daily_size,
        }
    )
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

    with pytest.raises(TargetSeriesError, match="gap span endpoint shares differ"):
        build_matrixport_targets(
            base,
            manifest,
            nav,
            holdings,
            subscriptions,
            "P1",
            "Robuxio",
            daily_holdings_path=daily_holdings,
        )


def test_matrixport_target_rejects_subscription_inserted_inside_gap_span(
    tmp_path: Path,
) -> None:
    """A declared missing NAV date cannot conceal a successful funding event."""

    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    _write(
        nav,
        "product_id,timestamp_utc,nav\n"
        "P1,2026-01-01T00:00:00Z,1\n"
        "P1,2026-01-02T00:00:00Z,1.1\n"
        "P1,2026-01-04T00:00:00Z,1.2\n",
    )
    _rebind_matrixport_nav(manifest, nav, declared_dates=("2026-01-03",))
    daily_holdings = _rebind_matrixport_daily_holdings(
        manifest, nav, declared_dates=("2026-01-03",)
    )
    subscription_hash, subscription_size = _write(
        subscriptions,
        "confirm_nav_time_utc,product_id,status,subscription_share,subscription_time_utc\n"
        "2026-01-02T00:00:00+00:00,P1,300,100,2026-01-01T21:00:00+00:00\n"
        "2026-01-03T00:00:00+00:00,P1,300,1,2026-01-03T00:00:00+00:00\n",
    )
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    subscription_artifact = next(
        row
        for row in manifest_value["artifacts"]
        if row["artifact_id"] == "csv/subscription_details.csv"
    )
    subscription_artifact["sha256"] = subscription_hash
    subscription_artifact["size_bytes"] = subscription_size
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

    with pytest.raises(
        TargetSeriesError, match="gap span contains a subscription/funding event"
    ):
        build_matrixport_targets(
            base,
            manifest,
            nav,
            holdings,
            subscriptions,
            "P1",
            "Robuxio",
            daily_holdings_path=daily_holdings,
        )


def test_matrixport_target_rejects_undeclared_or_stale_gap(
    tmp_path: Path,
) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    _write(
        nav,
        "product_id,timestamp_utc,nav\n"
        "P1,2026-01-01T00:00:00Z,1\n"
        "P1,2026-01-02T00:00:00Z,1.1\n"
        "P1,2026-01-04T00:00:00Z,1.2\n"
        "P1,2026-01-05T00:00:00Z,1.3\n",
    )
    _rebind_matrixport_nav(manifest, nav, declared_dates=())
    with pytest.raises(TargetSeriesError, match="NAV gaps differ"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )

    _rebind_matrixport_nav(manifest, nav, declared_dates=("2026-01-03", "2026-01-06"))
    with pytest.raises(TargetSeriesError, match="NAV gaps differ"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )


def test_matrixport_target_rejects_mismatched_or_duplicate_gap_authority(
    tmp_path: Path,
) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    exclusions = _matrixport_exclusions("2026-01-03")
    exclusions.pop()
    manifest_value["coverage"]["declared_exclusions"] = exclusions
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    with pytest.raises(TargetSeriesError, match="date sets differ"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )

    manifest_value["coverage"]["declared_exclusions"] = [
        *_matrixport_exclusions("2026-01-03"),
        _matrixport_exclusions("2026-01-03")[0],
    ]
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    with pytest.raises(TargetSeriesError, match="duplicated"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )

    manifest_value["coverage"]["declared_exclusions"] = _matrixport_exclusions(
        "2026-01-03"
    )
    manifest_value["coverage"]["declared_exclusions"][0]["provenance"] = "unapproved"
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    with pytest.raises(TargetSeriesError, match="authority differs"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )


def test_matrixport_target_rejects_manifest_drift(tmp_path: Path) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    nav.write_text(nav.read_text(encoding="utf-8") + "P1,2026-01-04T00:00:00Z,1\n")
    with pytest.raises(TargetSeriesError, match="differs from the manifest"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )


def test_matrixport_target_requires_matching_live_holding(tmp_path: Path) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    with pytest.raises(TargetSeriesError, match="exactly one live holding"):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "UNKNOWN", "Robuxio"
        )


def test_matrixport_target_rejects_assignment_without_successful_funding(
    tmp_path: Path,
) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    subscription_hash, subscription_size = _write(
        subscriptions,
        "confirm_nav_time_utc,product_id,status,subscription_share,subscription_time_utc\n"
        "2026-01-02T00:00:00+00:00,P1,299,100,2026-01-01T21:00:00+00:00\n",
    )
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest_value["artifacts"]
        if row["artifact_id"] == "csv/subscription_details.csv"
    )
    artifact["sha256"] = subscription_hash
    artifact["size_bytes"] = subscription_size
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

    with pytest.raises(
        TargetSeriesError, match="lacks a successful positive subscription"
    ):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )


def test_matrixport_target_requires_exact_held_nav_at_funding_confirmation(
    tmp_path: Path,
) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    subscription_hash, subscription_size = _write(
        subscriptions,
        "confirm_nav_time_utc,product_id,status,subscription_share,subscription_time_utc\n"
        "2026-01-04T00:00:00+00:00,P1,300,100,2026-01-01T21:00:00+00:00\n",
    )
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest_value["artifacts"]
        if row["artifact_id"] == "csv/subscription_details.csv"
    )
    artifact["sha256"] = subscription_hash
    artifact["size_bytes"] = subscription_size
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

    with pytest.raises(
        TargetSeriesError, match="funding confirmation lacks an exact held NAV"
    ):
        build_matrixport_targets(
            base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
        )


def test_matrixport_target_can_replace_one_generation_bound_series(
    tmp_path: Path,
) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["as_of_date"] = "2026-01-03"
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    base.write_text(
        base.read_text(encoding="utf-8")
        + "Robuxio,2025-12-31,0.5,ACCOUNT_LIVE,NET,TOTAL,\n",
        encoding="utf-8",
    )

    targets, proof_bytes = build_matrixport_targets(
        base,
        manifest,
        nav,
        holdings,
        subscriptions,
        "P1",
        "Robuxio",
        replace_existing=True,
    )
    rows = list(csv.DictReader(io.StringIO(targets.decode("utf-8"))))
    robuxio = [row for row in rows if row["series_id"] == "Robuxio"]
    assert [row["date"] for row in robuxio] == ["2026-01-03"]
    proof = json.loads(proof_bytes)
    assert proof["replacement"] == {
        "enabled": True,
        "prior_observed_end": "2025-12-31",
        "prior_observed_start": "2025-12-31",
        "removed_observations": 1,
    }

    evidence_path = tmp_path / "strategy-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-01",
                "schema_version": 1,
                "strategies": [{"strategy_id": "other", "strategy_name": "Other"}],
            }
        ),
        encoding="utf-8",
    )
    evidence = json.loads(
        build_matrixport_strategy_evidence(evidence_path, proof_bytes, "robuxio-crypto")
    )
    assert evidence["as_of_date"] == "2026-01-03"
    matrixport = next(
        row for row in evidence["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    assert matrixport["account"]["observed_end"] == "2026-01-03"
    assert matrixport["generation_id"] == "a" * 64


def test_decomposition_inputs_rebind_exact_sources(tmp_path: Path) -> None:
    base, manifest, nav, holdings, subscriptions = _sources(tmp_path)
    targets, proof = build_matrixport_targets(
        base, manifest, nav, holdings, subscriptions, "P1", "Robuxio"
    )
    factors = tmp_path / "factors.csv"
    factor_hash, factor_size = _write(factors, "date,RF,F1\n2026-01-01,0,0\n")
    base_hash = hashlib.sha256(base.read_bytes()).hexdigest()
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-01",
                "factor_source": {
                    "expected_sha256": factor_hash,
                    "expected_size_bytes": factor_size,
                },
                "target_source": {
                    "expected_sha256": base_hash,
                    "expected_size_bytes": base.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    rebound, copied_factors, copied_manifest = build_decomposition_inputs(
        spec, factors, base, manifest, targets, proof
    )
    value = json.loads(rebound)
    assert value["as_of_date"] == "2026-01-03"
    assert (
        value["target_source"]["expected_sha256"] == hashlib.sha256(targets).hexdigest()
    )
    assert value["target_source"]["schema_version"] == "2"
    assert len(value["provenance_sources"]) == 2
    assert copied_factors == factors.read_bytes()
    assert copied_manifest == manifest.read_bytes()


def test_quantbase_account_path_is_flow_adjusted_and_deterministic(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.csv"
    _write(
        base,
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "Existing,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n",
    )
    history = tmp_path / "history.csv"
    history_hash, history_size = _write(
        history,
        "time,value,cost_basis,profit_loss\n"
        "01/01/2026,0,0,0\n"
        "01/02/2026,100,100,0\n"
        "01/03/2026,110,100,10\n"
        "01/04/2026,220,200,20\n",
    )
    activity = tmp_path / "activity.csv"
    activity_hash, activity_size = _write(
        activity,
        "activity_type,amount,fund,is_rebalance,time,type\n"
        "filled_txn,100,107,false,2026-01-01T12:00:00Z,BUY\n"
        "filled_txn,100,107,false,2026-01-03T12:00:00Z,BUY\n",
    )
    manifest = tmp_path / "quantbase-generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "b" * 64,
                "as_of_date": "2026-01-04",
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
                        "sha256": history_hash,
                        "size_bytes": history_size,
                    },
                    {
                        "artifact_id": "source/csv/account_activity.csv",
                        "sha256": activity_hash,
                        "size_bytes": activity_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    targets, proof_bytes = build_quantbase_targets(
        base, manifest, history, activity, "107", "Volatis Strategy"
    )
    again, again_proof = build_quantbase_targets(
        base, manifest, history, activity, "107", "Volatis Strategy"
    )

    assert targets == again
    assert proof_bytes == again_proof
    rows = [
        row
        for row in csv.DictReader(io.StringIO(targets.decode("utf-8")))
        if row["series_id"] == "Volatis Strategy"
    ]
    assert [row["date"] for row in rows] == ["2026-01-03", "2026-01-04"]
    assert [row["return"] for row in rows] == ["0.1", "0.090909090909090909090909091"]
    proof = json.loads(proof_bytes)
    assert proof["coverage"]["flow_events"] == 2
    assert proof["coverage"]["return_observations"] == 2
    assert proof["evidence_layer"] == "ACCOUNT_LIVE"


def test_quantbase_independent_recalculation_selects_recalculated_authority() -> None:
    ledger_rows = [
        {
            "series_id": "Volatis Strategy",
            "date": "2026-01-02",
            "return": "-0.06",
            "evidence_layer": "ACCOUNT_LIVE",
            "return_basis": "NET",
            "return_type": "TOTAL",
            "cost": "",
        },
        {
            "series_id": "Volatis Strategy",
            "date": "2026-01-03",
            "return": "0.0200",
            "evidence_layer": "ACCOUNT_LIVE",
            "return_basis": "NET",
            "return_type": "TOTAL",
            "cost": "",
        },
    ]
    independently_recalculated = [
        {**ledger_rows[0], "return": "-0.060"},
        {**ledger_rows[1], "return": "0.02"},
    ]

    selected = target_series._quantbase_account_live_from_ledger(
        ledger_rows, independently_recalculated, set()
    )

    assert selected == independently_recalculated


@pytest.mark.parametrize(
    ("recalculated_rows", "message"),
    [
        (
            [
                {
                    "date": "2026-01-02",
                    "return": "-0.07",
                }
            ],
            "differs from provider ledger",
        ),
        (
            [
                {
                    "date": "2026-01-03",
                    "return": "-0.06",
                }
            ],
            "date perimeter differs",
        ),
    ],
)
def test_quantbase_independent_recalculation_rejects_ledger_drift(
    recalculated_rows: list[dict[str, str]], message: str
) -> None:
    ledger_rows = [
        {
            "series_id": "Volatis Strategy",
            "date": "2026-01-02",
            "return": "-0.06",
            "evidence_layer": "ACCOUNT_LIVE",
            "return_basis": "NET",
            "return_type": "TOTAL",
            "cost": "",
        }
    ]

    with pytest.raises(TargetSeriesError, match=message):
        target_series._quantbase_account_live_from_ledger(
            ledger_rows, recalculated_rows, set()
        )


def test_quantbase_account_path_rejects_manifest_drift(tmp_path: Path) -> None:
    base = tmp_path / "base.csv"
    _write(
        base,
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "Existing,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n",
    )
    history = tmp_path / "history.csv"
    history_hash, history_size = _write(
        history,
        "time,value,cost_basis,profit_loss\n"
        "01/01/2026,100,100,0\n"
        "01/02/2026,101,100,1\n",
    )
    activity = tmp_path / "activity.csv"
    activity_hash, activity_size = _write(
        activity,
        "activity_type,amount,fund,is_rebalance,time,type\n"
        "filled_txn,100,107,false,2026-01-01T12:00:00Z,BUY\n",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "b" * 64,
                "as_of_date": "2026-01-02",
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
                        "sha256": history_hash,
                        "size_bytes": history_size,
                    },
                    {
                        "artifact_id": "source/csv/account_activity.csv",
                        "sha256": activity_hash,
                        "size_bytes": activity_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    history.write_text(history.read_text(encoding="utf-8") + "01/03/2026,102,100,2\n")
    with pytest.raises(TargetSeriesError, match="differs from the manifest"):
        build_quantbase_targets(
            base, manifest, history, activity, "107", "Volatis Strategy"
        )


def test_portfolio123_provider_and_account_paths_remain_separate(
    tmp_path: Path,
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    targets = (
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "AI Factor [P123],2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n"
        "AI Factor [P123],2026-01-03,-0.02,ACCOUNT_LIVE,NET,TOTAL,\n"
    ).encode("utf-8")
    equity = tmp_path / "equity.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_entity_id": "provider-synthetic",
                    "Date": "2026-01-01",
                    "Total Equity": 100.0,
                    "Cash Added": 0.0,
                },
                {
                    "_entity_id": "provider-synthetic",
                    "Date": "2026-01-02",
                    "Total Equity": 102.0,
                    "Cash Added": 0.0,
                },
                {
                    "_entity_id": "provider-synthetic",
                    "Date": "2026-01-03",
                    "Total Equity": 101.0,
                    "Cash Added": 0.0,
                },
            ]
        ),
        equity,
    )
    metadata = tmp_path / "metadata.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "id": "account-synthetic",
                    "lastFix": 1767484800000,
                    "netWorth": 48562.85,
                    "netWorthPrevWithCashAdded": 48562.85,
                    "isLive": True,
                },
                {
                    "id": "account-synthetic",
                    "lastFix": 1767484800000,
                    "netWorth": 48562.85,
                    "netWorthPrevWithCashAdded": 48562.85,
                    "isLive": True,
                },
            ]
        ),
        metadata,
    )
    equity_hash = hashlib.sha256(equity.read_bytes()).hexdigest()
    metadata_hash = hashlib.sha256(metadata.read_bytes()).hexdigest()
    manifest = tmp_path / "portfolio123-generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "c" * 64,
                "as_of_date": "2026-01-05",
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "tables/har_equity_curve.parquet",
                        "sha256": equity_hash,
                        "size_bytes": equity.stat().st_size,
                    },
                    {
                        "artifact_id": "tables/har_account_metadata.parquet",
                        "sha256": metadata_hash,
                        "size_bytes": metadata.stat().st_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    evidence_bytes = build_portfolio123_evidence(
        targets,
        manifest,
        equity,
        metadata,
        "provider-synthetic",
        "account-synthetic",
        "p123-ai-factor",
        "AI Factor [P123]",
    )
    evidence = json.loads(evidence_bytes)

    assert evidence["account"]["observed_end"] == "2026-01-03"
    assert evidence["provider"]["observed_end"] == "2026-01-03"
    assert evidence["provider"]["return_observations"] == 2
    assert evidence["overlap"]["observations"] == 2
    assert evidence["evidence_layer"] == ("PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE")
    combined = json.loads(
        build_strategy_evidence(
            json.dumps(
                {
                    "coverage": {
                        "observed_end": "2026-01-04",
                        "observed_start": "2026-01-03",
                        "return_observations": 1,
                    },
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": "d" * 64,
                    "generation_as_of_date": "2026-01-04",
                    "provider_id": "quantbase",
                    "series_id": "Volatis Strategy",
                    "sources": {},
                    "strategy_id": "volatis-strategy",
                }
            ).encode("utf-8"),
            evidence_bytes,
        )
    )
    assert combined["as_of_date"] == "2026-01-05"
    assert [row["strategy_id"] for row in combined["strategies"]] == [
        "volatis-strategy",
        "p123-ai-factor",
    ]


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("lastFix", 1767571200000),
        ("netWorth", 48563.85),
        ("netWorthPrevWithCashAdded", 48561.85),
        ("isLive", False),
    ],
)
def test_portfolio123_snapshot_rejects_conflicting_duplicate_rows(
    tmp_path: Path, changed_field: str, changed_value: object
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    metadata = tmp_path / "metadata.parquet"
    original = {
        "id": "account-synthetic",
        "lastFix": 1767484800000,
        "netWorth": 48562.85,
        "netWorthPrevWithCashAdded": 48562.85,
        "isLive": True,
    }
    pq.write_table(
        pa.Table.from_pylist([original, {**original, changed_field: changed_value}]),
        metadata,
    )

    with pytest.raises(TargetSeriesError, match="snapshot is not unique"):
        target_series._portfolio123_snapshot(metadata, "account-synthetic")


def test_portfolio123_snapshot_rejects_absent_selected_account(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    metadata = tmp_path / "metadata.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "id": "different-account",
                    "lastFix": 1767484800000,
                    "netWorth": 48562.85,
                    "netWorthPrevWithCashAdded": 48562.85,
                    "isLive": True,
                }
            ]
        ),
        metadata,
    )

    with pytest.raises(TargetSeriesError, match="snapshot is not unique"):
        target_series._portfolio123_snapshot(metadata, "account-synthetic")


def test_layered_correlation_uses_generation_bound_live_sim_twin(
    tmp_path: Path,
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    targets = (
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "AI Factor [P123],2026-01-05,0.01,ACCOUNT_LIVE,NET,TOTAL,\n"
        "AI Factor [P123],2026-01-06,-0.02,ACCOUNT_LIVE,NET,TOTAL,\n"
        "Other,2026-01-05,0.005,ACCOUNT_LIVE,NET,TOTAL,\n"
        "Other,2026-01-06,0.006,ACCOUNT_LIVE,NET,TOTAL,\n"
    ).encode("utf-8")
    base_returns = tmp_path / "base-returns.csv"
    base_returns.write_bytes(
        b"series_id,date,return,evidence_layer\n"
        b"AI Factor [P123],2026-01-05,0.01,ACCOUNT_LIVE\n"
    )
    base_hash = hashlib.sha256(base_returns.read_bytes()).hexdigest()
    base_spec = tmp_path / "base-spec.json"
    base_spec.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-06",
                "return_source": {
                    "expected_sha256": base_hash,
                    "expected_size_bytes": base_returns.stat().st_size,
                },
                "schema_version": 1,
                "series_ids": ["AI Factor [P123]", "Other", "Missing"],
            }
        ),
        encoding="utf-8",
    )
    returns_path = tmp_path / "returns.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_id": "sim-synthetic",
                    "period_date": "2026-01-01",
                    "model_return": 100.0,
                    "source": "synthetic",
                },
                {
                    "entity_id": "sim-synthetic",
                    "period_date": "2026-01-02",
                    "model_return": 101.0,
                    "source": "synthetic",
                },
                {
                    "entity_id": "sim-synthetic",
                    "period_date": "2026-01-05",
                    "model_return": 99.99,
                    "source": "synthetic",
                },
            ]
        ),
        returns_path,
    )
    relationships_path = tmp_path / "relationships.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_ref_a": "provider-synthetic",
                    "entity_ref_b": "sim-synthetic",
                    "relationship_type": "live_sim_twin",
                    "notes": "SYNTHETIC explicit relationship",
                }
            ]
        ),
        relationships_path,
    )
    manifest = tmp_path / "generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "e" * 64,
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "tables/returns_daily.parquet",
                        "sha256": hashlib.sha256(returns_path.read_bytes()).hexdigest(),
                        "size_bytes": returns_path.stat().st_size,
                    },
                    {
                        "artifact_id": "tables/strategy_relationships.parquet",
                        "sha256": hashlib.sha256(
                            relationships_path.read_bytes()
                        ).hexdigest(),
                        "size_bytes": relationships_path.stat().st_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    strategy_evidence = json.dumps(
        {
            "strategies": [
                {
                    "account": {"provider_id": "ibkr"},
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": "f" * 64,
                    "overlap": {
                        "observations": 2,
                        "status": "TESTED_EXACT_DATE_PATHS",
                    },
                    "provider": {
                        "entity_id": "provider-synthetic",
                        "generation_id": "e" * 64,
                        "provider_id": "portfolio123",
                    },
                    "provider_id": "ibkr",
                    "strategy_name": "AI Factor [P123]",
                }
            ]
        },
        sort_keys=True,
    ).encode("utf-8")
    history_returns = tmp_path / "strategy-history.csv"
    history_returns.write_text(
        "series_id,date,return,evidence_layer,strategy_version\n"
        "Other,2025-12-30,0.02,BACKTEST,other-current:v1\n"
        "Other,2025-12-31,-0.01,BACKTEST,other-current:v1\n",
        encoding="utf-8",
    )
    history_manifest = tmp_path / "strategy-history.json"
    history_manifest.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-06",
                "builder": "portfolio-strategy-history",
                "builder_version": "1.4.0",
                "bundle_id": "b" * 64,
                "returns_artifact": {
                    "artifact_id": "strategy-history/returns.csv",
                    "sha256": hashlib.sha256(history_returns.read_bytes()).hexdigest(),
                    "size_bytes": history_returns.stat().st_size,
                },
                "schema_version": 1,
                "history_dispositions": [
                    {
                        "history_status": (
                            "CAPTURED_EXACT_VERSION_PROVIDER_LIVE_RETURN_TRANSFORM_DERIVED_NOT_ADMITTED"
                        ),
                        "implementation_reconciliation_status": (
                            "BLOCKED_RETURN_TRANSFORM; SEPARATE_FROM_ACCOUNT_LIVE"
                        ),
                        "reason": (
                            "Exact provider-live history is not an exact-version "
                            "backtest and remains comparison only."
                        ),
                        "required_for_analysis": True,
                        "series_id": "Missing",
                        "status": (
                            "CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_TRANSFORM_DERIVED_NOT_ADMITTED"
                        ),
                        "strategy_version": "missing-current:v1",
                    }
                ],
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
                        "series_id": "Other",
                        "strategy_version": "other-current:v1",
                    }
                ],
                "verification": "VERIFIED",
            }
        ),
        encoding="utf-8",
    )

    spec_bytes, returns_bytes, proof_bytes, manifest_bytes = (
        build_layered_correlation_inputs(
            base_spec,
            base_returns,
            targets,
            manifest,
            returns_path,
            relationships_path,
            "provider-synthetic",
            "sim-synthetic",
            "AI Factor [P123]",
            strategy_evidence,
            "2026-01-06",
            history_manifest,
            history_returns,
        )
    )
    spec = json.loads(spec_bytes)
    proof = json.loads(proof_bytes)
    rows = list(csv.DictReader(io.StringIO(returns_bytes.decode("utf-8"))))
    ai = [row for row in rows if row["series_id"] == "AI Factor [P123]"]
    backtest = [row for row in ai if row["evidence_layer"] == "BACKTEST"]
    live = [row for row in ai if row["evidence_layer"] == "ACCOUNT_LIVE"]
    other = [row for row in rows if row["series_id"] == "Other"]

    assert spec["schema_version"] == 2
    assert spec["return_source"]["schema_version"] == "2"
    assert tuple(rows[0]) == target_series.CORRELATION_FIELDS_V2
    assert len(backtest) == 2
    assert float(backtest[0]["return"]) == pytest.approx(0.01)
    assert float(backtest[1]["return"]) == pytest.approx(-0.01)
    assert {row["strategy_version"] for row in [*backtest, *live]} == {
        next(
            item["strategy_version"]
            for item in spec["evidence_mode_series"]
            if item["series_id"] == "AI Factor [P123]"
        )
    }
    assert len([row for row in other if row["evidence_layer"] == "BACKTEST"]) == 2
    assert {row["strategy_version"] for row in other} == {"other-current:v1"}
    # The live identity is the accepted-live-path hash over the two Other live
    # rows (hardcoded oracle: sha256 of the canonical JSON rows payload), while
    # the full-series version still binds the declared history.
    other_entry = next(
        item for item in spec["evidence_mode_series"] if item["series_id"] == "Other"
    )
    assert other_entry["strategy_version"] == "other-current:v1"
    assert other_entry["live_version_ref"] == (
        "accepted-live-path:e3b144a350688907d385a9fa93e5925e562327363cdf9d2c0242b104aa1a9ae8"
    )
    # The Portfolio123 sim-twin series uses the same uniform live identity:
    # the accepted-live-path hash over its live rows, not the sim-twin binding.
    ai_entry = next(
        item
        for item in spec["evidence_mode_series"]
        if item["series_id"] == "AI Factor [P123]"
    )
    assert ai_entry["strategy_version"].startswith("p123-live-sim-twin:")
    assert ai_entry["live_version_ref"] == (
        "accepted-live-path:6b9b3e5b57d81ad1916ca298654741bc7bb9e33c052f0afa7e384ec7bf7d3b47"
    )
    other_mapping = next(
        item for item in proof["series_mappings"] if item["series_id"] == "Other"
    )
    assert other_mapping["history_status"] == ("ACCEPTED_CURRENT_SPECIFICATION_HISTORY")
    assert other_mapping["implementation_reconciliation_status"] == "UNRESOLVED"
    missing = next(
        item for item in proof["series_mappings"] if item["series_id"] == "Missing"
    )
    assert missing["live_observations"] == 0
    assert missing["status"] == (
        "CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_TRANSFORM_DERIVED_NOT_ADMITTED"
    )
    assert missing["history_disposition_reason"] == (
        "Exact provider-live history is not an exact-version backtest and remains "
        "comparison only."
    )
    assert proof["simulation_coverage"]["relationship_type"] == "live_sim_twin"
    assert proof["strategy_history_bundle"]["verification"] == "VERIFIED"
    assert manifest_bytes == manifest.read_bytes()

    stale_history_manifest = json.loads(history_manifest.read_text(encoding="utf-8"))
    stale_history_manifest["builder_version"] = "1.1.0"
    history_manifest.write_text(json.dumps(stale_history_manifest), encoding="utf-8")
    with pytest.raises(
        TargetSeriesError, match="strategy history builder version is unsupported"
    ):
        target_series._strategy_history_returns(history_manifest, history_returns)

    wrong_relationship = tmp_path / "wrong-relationships.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_ref_a": "provider-synthetic",
                    "entity_ref_b": "sim-synthetic",
                    "relationship_type": "book_component",
                    "notes": "SYNTHETIC wrong relationship",
                }
            ]
        ),
        wrong_relationship,
    )
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["artifacts"][1].update(
        {
            "sha256": hashlib.sha256(wrong_relationship.read_bytes()).hexdigest(),
            "size_bytes": wrong_relationship.stat().st_size,
        }
    )
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    with pytest.raises(TargetSeriesError, match="version mapping is not unique"):
        build_layered_correlation_inputs(
            base_spec,
            base_returns,
            targets,
            manifest,
            returns_path,
            wrong_relationship,
            "provider-synthetic",
            "sim-synthetic",
            "AI Factor [P123]",
            strategy_evidence,
            "2026-01-06",
        )


def _provider_ledger_fixture(tmp_path: Path) -> tuple[Path, Path]:
    catalog = tmp_path / "provider-ledger.json"
    catalog.write_text(
        json.dumps(
            {
                "as_of_date": "2026-07-31",
                "catalog_schema_version": 1,
                "strategy_evidence": [
                    {
                        "account": {
                            "observed_end": "2026-07-31",
                            "observed_start": "2026-07-30",
                            "provider_id": "matrixport",
                            "return_observations": 2,
                        },
                        "evidence_layer": "ACCOUNT_LIVE",
                        "generation_id": "a" * 64,
                        "provider_id": "matrixport",
                        "strategy_id": "robuxio-crypto",
                        "strategy_name": "Robuxio",
                    }
                ],
                "strategy_returns": [
                    {
                        "cost": None,
                        "date": "2026-07-30",
                        "evidence_layer": "ACCOUNT_LIVE",
                        "provider_id": "matrixport",
                        "return": "0.01",
                        "return_basis": "NET",
                        "return_type": "TOTAL",
                        "strategy_id": "robuxio-crypto",
                        "strategy_name": "Robuxio",
                    },
                    {
                        "cost": None,
                        "date": "2026-07-31",
                        "evidence_layer": "ACCOUNT_LIVE",
                        "provider_id": "matrixport",
                        "return": "-0.02",
                        "return_basis": "NET",
                        "return_type": "TOTAL",
                        "strategy_id": "robuxio-crypto",
                        "strategy_name": "Robuxio",
                    },
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "generation.json"
    manifest.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "normalized/provider-ledger.json",
                        "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
                        "size_bytes": catalog.stat().st_size,
                    }
                ],
                "as_of_date": "2026-07-31",
                "generation_id": "b" * 64,
                "verification": "VERIFIED",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest, catalog


def test_provider_ledger_targets_preserve_one_canonical_series_contract(
    tmp_path: Path,
) -> None:
    manifest, catalog = _provider_ledger_fixture(tmp_path)

    targets, evidence, proof, manifest_bytes = build_provider_ledger_targets(
        manifest, catalog
    )

    rows = list(csv.DictReader(io.StringIO(targets.decode("utf-8"))))
    assert tuple(rows[0]) == target_series.TARGET_FIELDS
    assert [row["return"] for row in rows] == ["0.01", "-0.02"]
    assert {row["evidence_layer"] for row in rows} == {"ACCOUNT_LIVE"}
    contract = json.loads(proof)["series_contracts"][0]
    assert contract == {
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": "a" * 64,
        "observed_end": "2026-07-31",
        "observed_start": "2026-07-30",
        "provider_id": "matrixport",
        "return_observations": 2,
        "series_id": "Robuxio",
        "strategy_id": "robuxio-crypto",
    }
    assert json.loads(evidence)["provider_ledger_generation_id"] == "b" * 64
    assert manifest_bytes == manifest.read_bytes()


def _refresh_provider_ledger_fixture_manifest(manifest: Path, catalog: Path) -> None:
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["artifacts"][0].update(
        {
            "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
            "size_bytes": catalog.stat().st_size,
        }
    )
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")


def test_provider_ledger_targets_select_declared_layer_from_mixed_returns(
    tmp_path: Path,
) -> None:
    manifest, catalog = _provider_ledger_fixture(tmp_path)
    value = json.loads(catalog.read_text(encoding="utf-8"))
    provider_rows = []
    for row in value["strategy_returns"]:
        provider_row = dict(row)
        provider_row["evidence_layer"] = "PROVIDER_LIVE"
        provider_row["return"] = "0.50"
        provider_rows.append(provider_row)
    value["strategy_returns"].extend(provider_rows)
    catalog.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_provider_ledger_fixture_manifest(manifest, catalog)

    targets, _evidence, proof, _manifest_bytes = build_provider_ledger_targets(
        manifest, catalog
    )

    rows = list(csv.DictReader(io.StringIO(targets.decode("utf-8"))))
    assert [row["return"] for row in rows] == ["0.01", "-0.02"]
    assert {row["evidence_layer"] for row in rows} == {"ACCOUNT_LIVE"}
    assert json.loads(proof)["series_contracts"][0]["return_observations"] == 2


def test_provider_ledger_targets_reject_selected_layer_statistics_mismatch(
    tmp_path: Path,
) -> None:
    manifest, catalog = _provider_ledger_fixture(tmp_path)
    value = json.loads(catalog.read_text(encoding="utf-8"))
    value["strategy_returns"][1]["evidence_layer"] = "PROVIDER_LIVE"
    catalog.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_provider_ledger_fixture_manifest(manifest, catalog)

    with pytest.raises(TargetSeriesError, match="evidence statistics differ"):
        build_provider_ledger_targets(manifest, catalog)


def test_provider_ledger_targets_reject_duplicate_within_selected_layer(
    tmp_path: Path,
) -> None:
    manifest, catalog = _provider_ledger_fixture(tmp_path)
    value = json.loads(catalog.read_text(encoding="utf-8"))
    value["strategy_returns"].append(dict(value["strategy_returns"][0]))
    catalog.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_provider_ledger_fixture_manifest(manifest, catalog)

    with pytest.raises(TargetSeriesError, match="strategy return is duplicated"):
        build_provider_ledger_targets(manifest, catalog)


@pytest.mark.parametrize("selection", [None, "UNDECLARED_LAYER"])
def test_provider_ledger_targets_reject_missing_or_invalid_layer_selection(
    tmp_path: Path, selection: str | None
) -> None:
    manifest, catalog = _provider_ledger_fixture(tmp_path)
    value = json.loads(catalog.read_text(encoding="utf-8"))
    if selection is None:
        value["strategy_evidence"][0].pop("evidence_layer")
    else:
        value["strategy_evidence"][0]["evidence_layer"] = selection
    catalog.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_provider_ledger_fixture_manifest(manifest, catalog)

    with pytest.raises(TargetSeriesError, match="selection is invalid"):
        build_provider_ledger_targets(manifest, catalog)


def test_provider_ledger_targets_reject_ambiguous_layer_selection(
    tmp_path: Path,
) -> None:
    manifest, catalog = _provider_ledger_fixture(tmp_path)
    value = json.loads(catalog.read_text(encoding="utf-8"))
    value["strategy_evidence"].append(dict(value["strategy_evidence"][0]))
    catalog.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_provider_ledger_fixture_manifest(manifest, catalog)

    with pytest.raises(TargetSeriesError, match="strategy evidence is ambiguous"):
        build_provider_ledger_targets(manifest, catalog)


def _p123_managed_fixture(
    tmp_path: Path,
    *,
    data: list[list[float]],
    weekdays: list[str],
) -> tuple[Path, Path, Path]:
    """Write the three tables the exact-connected-model transform reads."""

    import pyarrow as pa
    import pyarrow.parquet as pq

    metadata_row = {
        "assetName": "EU Multifactor Microcap",
        "assetUid": "exact-asset",
        "currency": "USD",
        "inceptionDt": "2025-11-12",
        "isAccountLive": "true",
        "name": "EU Multifactor Microcap",
        "portfolioUid": "exact-portfolio",
        "posCnt": "13",
        "strategyTotalHoldCount": "13",
    }
    metadata_path = tmp_path / "metadata.parquet"
    pq.write_table(pa.Table.from_pylist([metadata_row]), metadata_path)

    panels = [
        {"graphs": [{"idx": 0, "s": "Strategy"}]},
        {"graphs": [{"idx": 1, "s": "Total Value"}]},
    ]
    performance_path = tmp_path / "performance.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_entity_id": "exact-asset",
                    "field": "data",
                    "value": json.dumps(data),
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "date",
                    "value": weekdays[0],
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "dateLast",
                    "value": weekdays[-1],
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "panels",
                    "value": json.dumps(panels),
                },
            ]
        ),
        performance_path,
    )

    equity_path = tmp_path / "equity.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "Cash Added": row[2],
                    "Date": observed,
                    "Total Equity": row[1],
                    "_entity_id": "exact-asset",
                }
                for observed, row in zip(weekdays, data, strict=True)
            ]
        ),
        equity_path,
    )
    return metadata_path, performance_path, equity_path


def test_p123_provider_performance_returns_read_the_level_not_the_marker(
    tmp_path: Path,
) -> None:
    """Each observation is [marker, Strategy, Total Value].

    The leading element is a weekday marker outside the two-graph indexing that
    ``panels`` declares.  Reading it as a percentage collapses every return onto
    the handful of values the marker takes.  Real Portfolio123 markers are small
    day codes unrelated to the level, so this fixture keeps them apart: reading
    the markers 0/1/3/1 would compound to +1%, while the levels 100 -> 101 ->
    99 -> 99 compound to -1%.
    """

    data = [
        [0.0, 100.0, 100.0],
        [1.0, 101.0, 101.0],
        [3.0, 99.0, 99.0],
        [1.0, 99.0, 99.0],
    ]
    weekdays = ["2025-11-13", "2025-11-14", "2025-11-17", "2025-11-18"]
    metadata_path, performance_path, equity_path = _p123_managed_fixture(
        tmp_path, data=data, weekdays=weekdays
    )
    generation_id = "a" * 64
    accepted_version = target_series._strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": generation_id,
            "provider_entity_ids": ["exact-asset", "exact-portfolio"],
            "strategy_name": "EU Multifactor Microcap",
        },
    )

    returns, identity = target_series._portfolio123_provider_performance_returns(
        metadata_path=metadata_path,
        performance_path=performance_path,
        equity_path=equity_path,
        generation_id=generation_id,
        series_id="EU [P123]",
        accepted_version=accepted_version,
    )

    assert [row["date"] for row in returns] == weekdays[1:]
    assert [row["return"] for row in returns] == [
        "0.01",
        "-0.0198019801980198019801980198",
        "0",
    ]
    assert identity["raw_level_observations"] == 4
    assert identity["return_observations"] == 3
    assert identity["equity_overlap_observations"] == 4
    compounded = Decimal(1)
    for row in returns:
        compounded *= Decimal(1) + Decimal(row["return"])
    assert compounded - Decimal(1) == Decimal("-0.01")
