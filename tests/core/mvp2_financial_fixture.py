"""Source-backed seven-strategy fixtures for the MVP-2 vertical test.

This module deliberately composes the existing layered-history, planning, and
decomposition producers.  It does not replace any of their verification seams.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_STRATEGIES = {
    "p123-ai-factor": "AI Factor [P123]",
    "p123-eu": "EU [P123]",
    "qsi": "QSI",
    "tt-portfolio": "TT Portfolio",
    "volatis-strategy": "Volatis Strategy",
    "grizzlybears-vtma": "GrizzlyBears VTMA",
    "robuxio-crypto": "Robuxio Crypto (High Sharpe High Vol)",
}
SOURCE_FACTORS = {
    "us-economic": ("ECONOMIC_FACTOR", "economic", "us_factors", "MKT_RF"),
    "europe-economic": (
        "ECONOMIC_FACTOR",
        "economic",
        "europe_factors",
        "MKT_RF",
    ),
    "us-equity-style": ("ETF_STYLE", "style", "style_returns", "VALUE"),
    "multi-asset-style": ("ETF_STYLE", "style", "style_returns", "GROWTH"),
    "us-sector": ("RETURN_SECTOR", "sector", "sector_returns", "TECH"),
    "us-industry": ("RETURN_INDUSTRY", "industry", "us_industries", "INDUSTRY"),
}
EVIDENCE_MODES = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plan_tests = _load(
    "mvp2_current_plan_fixture",
    "tests/portfolio_analytics/test_portfolio_current_decomposition_plan.py",
)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    return path


def project_account_source(baseline: dict[str, Any], ledger: dict[str, Any]) -> None:
    """Project declared synthetic purchases and daily account NAVs with real kernels.

    Each account is initially bought for its first recorded NAV. There are no
    later deposits or withdrawals. A strategy reassignment changes ownership of
    its existing contributed basis without creating a broker transaction.
    """
    import provider_ledger

    accounts = {row["account_id"]: row for row in baseline["accounts"]}
    assignments = baseline["assignments"]
    navs = ledger["nav_observations"]
    ledger["accounting_currency"] = "USD"
    for nav in navs:
        matches = [
            row
            for row in assignments
            if row["account_id"] == nav["account_id"]
            and row["effective_start"] <= nav["as_of_date"]
            and (
                row["effective_end"] is None
                or nav["as_of_date"] <= row["effective_end"]
            )
        ]
        assert len(matches) == 1, (nav["record_id"], matches)
        nav.update(
            provider_id=accounts[nav["account_id"]]["provider"].lower(),
            strategy_id=matches[0]["strategy_id"],
            mapping_status="MAPPED",
        )
    first_by_account: dict[str, dict[str, Any]] = {}
    for nav in sorted(navs, key=lambda row: row["as_of_date"]):
        first_by_account.setdefault(nav["account_id"], nav)
    provider_generations = {
        row["provider_id"]: row["generation_id"]
        for row in ledger["provider_generations"]
    }
    ledger["capital_anchors"] = [
        {
            "record_id": f"synthetic-opening-purchase:{account_id}",
            "account_id": account_id,
            "provider_id": nav["provider_id"],
            "generation_id": provider_generations[nav["provider_id"]],
            "source_artifact_id": f"raw/{nav['provider_id']}.json",
            "source_record_id": f"synthetic-opening-purchase:{account_id}",
            "nav_source_artifact_id": f"raw/{nav['provider_id']}.json",
            "strategy_id": nav["strategy_id"],
            "capital_type": "OPENING_CAPITAL_ANCHOR",
            "mapping_status": "MAPPED",
            "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
            "evidence_layer": "ACCOUNT_LIVE",
            "event_date": nav["as_of_date"],
            "nav_recognition_date": nav["as_of_date"],
            "amount": dict(nav["nav"]),
            "opening_nav": dict(nav["nav"]),
            "authority_profile": "SYNTHETIC",
        }
        for account_id, nav in sorted(first_by_account.items())
    ]
    ledger["admissions"] = [
        {
            "account_id": account_id,
            "fact_type": "flows",
            "disposition": "AUTHORITATIVE",
            "effective_start": nav["as_of_date"],
            "effective_end": ledger["as_of_date"],
        }
        for account_id, nav in sorted(first_by_account.items())
    ]
    returns = provider_ledger._broker_strategy_returns(
        navs,
        ledger["flows"],
        {row["provider_id"] for row in navs},
        capital_anchors=ledger["capital_anchors"],
    )
    for row in returns:
        row["strategy_name"] = REQUIRED_STRATEGIES[row["strategy_id"]]
    ledger["strategy_returns"] = returns
    # Preserve the NAV-anchor facts required by the provider return kernel and
    # add distinct owner-funded purchase facts for the capital vertical. These
    # capital-only facts are not mapped broker flows and therefore cannot alter
    # the provider return calculation.
    ledger["flows"].extend(
        {
            **anchor,
            "record_id": anchor["record_id"].replace(
                "synthetic-opening-purchase:", "synthetic-owner-funded-purchase:"
            ),
            "source_record_id": anchor["source_record_id"].replace(
                "synthetic-opening-purchase:", "synthetic-owner-funded-purchase:"
            ),
            "capital_type": "OPENING_CONTRIBUTED_BASIS",
            "flow_type": "EXTERNAL_CONTRIBUTION",
            "mapping_status": "UNMAPPED",
            "provider_account_scope": "ACCOUNT_ONLY",
            "strategy_id": None,
        }
        for anchor in ledger["capital_anchors"]
    )
    ledger["provider_coverage"] = [
        {
            "provider_id": provider_id,
            "generation_as_of_date": ledger["as_of_date"],
            "coverage": {
                "fact_types": [
                    "nav_observations",
                    "capital_anchors",
                    "strategy_returns",
                ],
                "observed_start": min(
                    row["as_of_date"]
                    for row in navs
                    if row["provider_id"] == provider_id
                ),
                "observed_end": max(
                    row["as_of_date"]
                    for row in navs
                    if row["provider_id"] == provider_id
                ),
                "status": "AVAILABLE",
            },
        }
        for provider_id in sorted({row["provider_id"] for row in navs})
    ]
    ledger["reconciliation_checks"] = []


def build_capital_vertical(
    root: Path, paths: dict[str, Path], *, registry_path: Path | None = None
) -> dict[str, Path]:
    """Recompute owner capital from the selected purchase facts and assignments."""
    import portfolio_invested_capital as capital

    root.mkdir(parents=True, exist_ok=True)
    contract_path = paths["contract"]
    contract = json.loads(contract_path.read_bytes())
    ledger_path = (
        paths["provider_generation"].parent / "normalized/provider-ledger.json"
    )
    baseline_path = (
        paths["baseline_generation"].parent / "normalized/portfolio-baseline.json"
    )
    ledger = json.loads(ledger_path.read_bytes())
    baseline = json.loads(baseline_path.read_bytes())
    # The selected baseline already carries these exact account assignments.
    provenance = {
        "as_of_date": baseline["as_of_date"],
        "accounts": baseline["accounts"],
        "assignments": [
            {**row, "mapping_status": "MAPPED"} for row in baseline["assignments"]
        ],
    }
    provenance_path = _write_json(root / "portfolio-provenance.json", provenance)
    spec_path = _write_json(
        root / "provider-ledger-spec.json",
        {
            "provenance": {
                "catalog_path": str(provenance_path),
                "catalog_sha256": capital._file_binding(provenance_path)[0]["sha256"],
                "generation_id": paths["baseline_generation"].parent.name,
            }
        },
    )
    epochs = [
        {
            "epoch_id": f"{sleeve['sleeve_alias']}:{assignment['effective_start']}",
            "sleeve_alias": sleeve["sleeve_alias"],
            "account_id": f"synthetic-account-{index:02d}",
            **{
                key: assignment[key]
                for key in ("strategy_id", "effective_start", "effective_end")
            },
        }
        for index, sleeve in enumerate(contract["current_census"]["account_sleeves"])
        for assignment in sleeve["assignments"]
        if assignment["strategy_id"] in REQUIRED_STRATEGIES
    ]
    opening = []
    for fact in (
        row
        for row in ledger["flows"]
        if row.get("capital_type") == "OPENING_CONTRIBUTED_BASIS"
    ):
        epoch = next(
            row
            for row in epochs
            if row["account_id"] == fact["account_id"]
            and row["effective_start"] == fact["nav_recognition_date"]
        )
        digest = capital._digest(fact)
        opening.append(
            {
                "event_id": fact["record_id"],
                "epoch_id": epoch["epoch_id"],
                "date": fact["nav_recognition_date"],
                "timestamp": None,
                "classification": "OPENING_CONTRIBUTED_BASIS",
                "amount": fact["amount"]["value"],
                "currency": "USD",
                "fx_rate": "1",
                "source_evidence_sha256": digest,
                "fx_evidence_sha256": digest,
            }
        )
    coverage = []
    for epoch in epochs:
        authority = next(
            row
            for row in ledger["admissions"]
            if row["account_id"] == epoch["account_id"]
        )
        coverage.append(
            {
                "epoch_id": epoch["epoch_id"],
                "start_date": epoch["effective_start"],
                "end_date": epoch["effective_end"] or ledger["as_of_date"],
                "evidence_sha256": capital._digest(authority),
            }
        )
    bindings = {
        "provider_ledger": capital._file_binding(ledger_path)[0],
        "provider_ledger_spec": capital._file_binding(spec_path)[0],
    }
    if registry_path is not None:
        bindings["strategy_registry"] = capital._file_binding(registry_path)[0]
    input_path = _write_json(
        root / "capital-input-generation.json",
        {
            "schema_version": 1,
            "authority_profile": "SYNTHETIC",
            "product_contract_sha256": hashlib.sha256(
                contract_path.read_bytes()
            ).hexdigest(),
            "policy_sha256": capital.invested_capital_policy_digest(contract),
            "required_epochs": epochs,
            "evaluation_date": "2026-09-05",
            "complete_through": ledger["as_of_date"],
            "opening_basis": opening,
            "events": [],
            "flow_coverage": coverage,
            "source_bindings": bindings,
            "correction_bindings": [],
        },
    )
    result_path = _write_json(
        root / "invested-capital.json",
        capital.build_governed_capital_result(input_path, contract_path),
    )
    return {
        "input": input_path,
        "result": result_path,
        "spec": spec_path,
        "provenance": provenance_path,
    }


def bind_native_history_sources(
    history_fixture: dict[str, Any], ledger: dict[str, Any]
) -> None:
    """Write native Quantbase/Matrixport rows from the exact selected account NAVs."""
    from decimal import Decimal

    history = plan_tests.history_tests
    contract = json.loads(history_fixture["contract_path"].read_bytes())
    for provider, strategy_id in (
        ("quantbase", "volatis-strategy"),
        ("matrixport", "robuxio-crypto"),
    ):
        navs = sorted(
            (
                row
                for row in ledger["nav_observations"]
                if row["strategy_id"] == strategy_id
            ),
            key=lambda row: row["as_of_date"],
        )
        assert len({row["account_id"] for row in navs}) == 1
        first_day = navs[0]["as_of_date"]
        opening = navs[0]["nav"]["value"]
        if provider == "quantbase":
            nav_path = history_fixture["quantbase_history_path"]
            activity_path = history_fixture["quantbase_activity_path"]
            history._write_csv(
                nav_path,
                ["time", "value", "cost_basis", "profit_loss"],
                [
                    {
                        "time": date.fromisoformat(row["as_of_date"]).strftime(
                            "%m/%d/%Y"
                        ),
                        "value": row["nav"]["value"],
                        "cost_basis": opening,
                        "profit_loss": str(
                            Decimal(row["nav"]["value"]) - Decimal(opening)
                        ),
                    }
                    for row in navs
                ],
            )
            history._write_csv(
                activity_path,
                ["activity_type", "amount", "fund", "is_rebalance", "time", "type"],
                [
                    {
                        "activity_type": "filled_txn",
                        "amount": opening,
                        "fund": history_fixture["quantbase_fund_id"],
                        "is_rebalance": "false",
                        "time": first_day + "T12:00:00Z",
                        "type": "BUY",
                    }
                ],
            )
            artifacts = [
                history._artifact(
                    nav_path, "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
                ),
                history._artifact(activity_path, "source/csv/account_activity.csv"),
            ]
            adapter = "quant-engine-web-contract-harvester"
            projected, _ = history.target_series._quantbase_returns(
                nav_path,
                activity_path,
                history_fixture["quantbase_fund_id"],
                REQUIRED_STRATEGIES[strategy_id],
            )
            correction_ids = ("volatis-provider-live", "volatis-account-live")
            next(
                row
                for row in contract["history_corrections"]["corrections"]
                if row["correction_id"] == "volatis-account-live"
            )["funding_event"]["event_date"] = first_day
        else:
            nav_path = history_fixture["matrixport_nav_path"]
            daily_path = history_fixture["matrixport_daily_holdings_path"]
            activity_path = history_fixture["matrixport_subscription_path"]
            product = history_fixture["matrixport_product_id"]
            # Two declared flat provider NAVs predate this synthetic purchase.
            # They establish provider history without extending account ownership.
            provider_navs = [
                {
                    "as_of_date": (
                        date.fromisoformat(first_day) - timedelta(days=offset)
                    ).isoformat(),
                    "nav": navs[0]["nav"],
                }
                for offset in (2, 1)
            ] + navs
            history._write_csv(
                nav_path,
                ["nav", "product_id", "timestamp_utc"],
                [
                    {
                        "nav": str(Decimal(row["nav"]["value"]) / 10),
                        "product_id": product,
                        "timestamp_utc": row["as_of_date"] + "T00:00:00Z",
                    }
                    for row in provider_navs
                ],
            )
            history._write_csv(
                daily_path,
                ["product_id", "utc_date", "share", "nav", "nav_available"],
                [
                    {
                        "product_id": product,
                        "utc_date": row["as_of_date"],
                        "share": "10",
                        "nav": str(Decimal(row["nav"]["value"]) / 10),
                        "nav_available": "true",
                    }
                    for row in navs
                ],
            )
            history._write_csv(
                activity_path,
                [
                    "confirm_nav_time_utc",
                    "product_id",
                    "status",
                    "subscription_share",
                    "subscription_time_utc",
                ],
                [
                    {
                        "confirm_nav_time_utc": first_day + "T12:00:00Z",
                        "product_id": product,
                        "status": "300",
                        "subscription_share": "10",
                        "subscription_time_utc": first_day + "T08:00:00Z",
                    }
                ],
            )
            artifacts = [
                history._artifact(nav_path, "csv/nav_daily.csv"),
                history._artifact(
                    history_fixture["matrixport_holdings_path"], "csv/holdings.csv"
                ),
                history._artifact(daily_path, "csv/daily_holdings.csv"),
                history._artifact(activity_path, "csv/subscription_details.csv"),
            ]
            adapter = "quant-engine-matrixport"
            source = next(
                row
                for row in contract["source_segments"]
                if row["source_id"] == "matrixport-account-generation"
            )
            source["declared_gap_policy"] = history._matrixport_gap_policy(
                nav_path=nav_path,
                holdings_path=history_fixture["matrixport_holdings_path"],
                subscription_path=activity_path,
                daily_holdings_path=daily_path,
                product_id=product,
            )
            projected, _ = history.target_series._matrixport_returns(
                nav_path,
                history_fixture["matrixport_holdings_path"],
                activity_path,
                daily_path,
                product,
                REQUIRED_STRATEGIES[strategy_id],
                frozenset(),
            )
            correction_ids = ("robuxio-account-live-after-funding",)
            provider_returns = history.target_series._matrixport_provider_returns(
                nav_path,
                product,
                REQUIRED_STRATEGIES[strategy_id],
                date.fromisoformat(first_day),
            )
            history._update_correction_interval(
                contract,
                "robuxio-provider-live-before-funding",
                observed_start=provider_navs[0]["as_of_date"],
                observed_end=provider_returns[-1]["date"],
                effective_start=provider_navs[0]["as_of_date"],
                effective_end=first_day,
            )
        expected = {
            row["date"]: Decimal(row["return"])
            for row in ledger["strategy_returns"]
            if row["strategy_id"] == strategy_id
        }
        actual = {row["date"]: Decimal(row["return"]) for row in projected}
        assert actual.keys() == expected.keys(), (
            provider,
            actual.keys() ^ expected.keys(),
        )
        assert all(
            abs(actual[day] - expected[day]) < Decimal("1e-25") for day in actual
        )
        manifest_path = history_fixture[f"{provider}_manifest_path"]
        generation = history._core_generation_manifest(
            manifest_path, adapter, ledger["as_of_date"], artifacts
        )
        history._pointer(
            history_fixture[f"{provider}_pointer_path"], generation, manifest_path
        )
        history._update_source_generation(
            contract,
            f"{provider}-account-generation",
            manifest_path,
            ledger["as_of_date"],
        )
        history._update_correction_generations(
            contract,
            f"{provider}-account-generation",
            manifest_path,
            ledger["as_of_date"],
            provider.upper(),
        )
        for correction_id in correction_ids:
            history._update_correction_interval(
                contract,
                correction_id,
                observed_start=min(actual),
                observed_end=max(actual),
                effective_start=first_day,
                effective_end=None,
            )
        provider_row = next(
            row
            for row in ledger["provider_generations"]
            if row["provider_id"] == provider
        )
        provider_row.update(
            generation_id=generation,
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        for anchor in ledger["capital_anchors"]:
            if anchor["provider_id"] == provider:
                anchor["generation_id"] = generation
    _write_json(history_fixture["contract_path"], contract)


def build_shared_history_source(
    root: Path, monkeypatch: Any
) -> tuple[dict[str, Any], dict[str, Path]]:
    """Prepare the existing history fixture from one selected account source set."""
    from copy import deepcopy

    history = plan_tests.history_tests
    history._install_synthetic_history_authority(monkeypatch)
    prepared = _install_hybrid_source(history._history_fixture(root / "history"))
    metadata = json.loads(prepared["provider_ledger_catalog_path"].read_bytes())[
        "strategy_evidence"
    ]
    baseline_helper = _load(
        "mvp2_baseline_fixture", "tests/core/mvp2_baseline_fixture.py"
    )
    hwm = _load(
        "mvp2_shared_hwm_fixture", "tests/portfolio_ledger/test_hwm_selected_inputs.py"
    )

    def project(baseline: dict[str, Any], ledger: dict[str, Any]) -> None:
        project_account_source(baseline, ledger)
        ledger["strategy_evidence"] = deepcopy(metadata)
        for row in ledger["strategy_evidence"]:
            returns = [
                item
                for item in ledger["strategy_returns"]
                if item["strategy_id"] == row["strategy_id"]
            ]
            row["account"] = {
                "observed_start": min(item["date"] for item in returns),
                "observed_end": max(item["date"] for item in returns),
                "return_observations": len(returns),
            }
            row["generation_as_of_date"] = ledger["as_of_date"]
            row["sources"]["account_returns"]["sha256"] = hashlib.sha256(
                _canonical(returns)
            ).hexdigest()
        bind_native_history_sources(prepared, ledger)
        baseline_helper.mutate_baseline(baseline, ledger)

    paths = hwm._fixture(
        root / "selected",
        base_contract_path=prepared["contract_path"],
        mutate_baseline=project,
        daily=True,
    )
    prepared["provider_ledger_catalog_path"] = (
        paths["provider_generation"].parent / "normalized/provider-ledger.json"
    )
    prepared["provider_ledger_manifest_path"] = paths["provider_generation"]
    history._pointer(
        prepared["provider_ledger_pointer_path"],
        paths["provider_generation"].parent.name,
        paths["provider_generation"],
    )
    contract = json.loads(prepared["contract_path"].read_bytes())
    history._update_source_generation(
        contract,
        "owner-account-live-history",
        paths["provider_generation"],
        prepared["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "owner-account-live-history",
        paths["provider_generation"],
        prepared["as_of_date"],
        "PROVIDER_LEDGER",
    )
    history._restore_retained_echo_correction(contract)
    _write_json(prepared["contract_path"], contract)
    selected_ledger = json.loads(prepared["provider_ledger_catalog_path"].read_bytes())
    live_starts = {
        strategy_id: min(
            row["date"]
            for row in selected_ledger["strategy_returns"]
            if row["strategy_id"] == strategy_id
        )
        for strategy_id in REQUIRED_STRATEGIES
    }
    eu_dates = [
        row["date"]
        for row in selected_ledger["strategy_returns"]
        if row["strategy_id"] == "p123-eu"
    ]
    contract = json.loads(prepared["contract_path"].read_bytes())
    history._update_correction_interval(
        contract,
        "p123-eu-account-live",
        observed_start=min(eu_dates),
        observed_end=max(eu_dates),
        effective_start=min(eu_dates),
        effective_end=None,
    )
    _write_json(prepared["contract_path"], contract)
    # The shared selected account source, not the intermediate fixture, owns
    # each live boundary.  Rebuild only explicitly synthetic prehistory through
    # the day before those actual selected boundaries.
    _install_hybrid_source(
        prepared,
        live_starts=live_starts,
        align_live_sources=False,
    )
    contract = json.loads(prepared["contract_path"].read_bytes())
    selected_contract = json.loads(paths["contract"].read_bytes())
    contract["contract_schema_version"] = 7
    contract["epoch_manifest_source"] = selected_contract["epoch_manifest_source"]
    # hwm._fixture(..., daily=True) emits a declared synthetic NAV for every
    # calendar date; this fixture-only source is therefore 24/7 daily, unlike
    # the generic production account/provider-publication declaration.
    owner_account_live = next(
        source
        for source in contract["source_segments"]
        if source["source_id"] == "owner-account-live-history"
    )
    owner_account_live["temporal"].update(calendar="ALWAYS_ON", cadence="DAILY")
    history._update_source_generation(
        contract,
        "owner-account-live-history",
        paths["provider_generation"],
        prepared["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "owner-account-live-history",
        paths["provider_generation"],
        prepared["as_of_date"],
        "PROVIDER_LEDGER",
    )
    ledger = json.loads(prepared["provider_ledger_catalog_path"].read_bytes())
    eu_dates = [
        row["date"]
        for row in ledger["strategy_returns"]
        if row["strategy_id"] == "p123-eu"
    ]
    history._update_correction_interval(
        contract,
        "p123-eu-account-live",
        observed_start=min(eu_dates),
        observed_end=max(eu_dates),
        effective_start=min(eu_dates),
        effective_end=None,
    )
    history._restore_retained_echo_correction(contract)
    _bind_selected_ai_version(prepared, contract)
    _write_json(prepared["contract_path"], contract)
    _write_json(
        root / "shared-source-paths.json",
        {
            "prepared": {
                key: str(value) if isinstance(value, Path) else value
                for key, value in prepared.items()
            },
            "selected": {key: str(value) for key, value in paths.items()},
        },
    )
    history._refresh_materialized_segment_identities(prepared)
    return prepared, paths


def _bind_selected_ai_version(
    fixture: dict[str, Any], contract: dict[str, Any]
) -> None:
    """Bind the AI twin version to the final selected ledger evidence bytes."""
    history = plan_tests.history_tests
    _targets, evidence_content, _proof, _manifest = (
        history.target_series.build_provider_ledger_targets(
            fixture["provider_ledger_manifest_path"],
            fixture["provider_ledger_catalog_path"],
        )
    )
    evidence = next(
        row
        for row in json.loads(evidence_content)["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    generation = json.loads(fixture["portfolio123_manifest_path"].read_bytes())
    _rows, version, _coverage = history.target_series._p123_ai_backtest(
        evidence=evidence,
        returns_path=fixture["portfolio123_returns_path"],
        relationships_path=fixture["portfolio123_relationships_path"],
        generation_id=generation["generation_id"],
        relationships_sha256=hashlib.sha256(
            fixture["portfolio123_relationships_path"].read_bytes()
        ).hexdigest(),
        strategy_evidence_sha256=hashlib.sha256(evidence_content).hexdigest(),
        series_id=REQUIRED_STRATEGIES["p123-ai-factor"],
    )
    for strategy in contract["strategies"]:
        if strategy["strategy_id"] == "p123-ai-factor":
            strategy["strategy_version"]["version_ref"] = version
    for source in contract["source_segments"]:
        versions = source.get("strategy_version", {}).get("version_refs", {})
        if "p123-ai-factor" in versions:
            versions["p123-ai-factor"] = version
    for correction in contract["history_corrections"]["corrections"]:
        if correction.get("strategy_id") == "p123-ai-factor":
            correction["strategy_version"]["version_ref"] = version
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        if identity["strategy_id"] == "p123-ai-factor":
            identity["strategy_version"] = version


def bind_final_hwm_contract(paths: dict[str, Path], contract_path: Path) -> None:
    """Freeze selected synthetic HWM authorities after history/plan binding."""
    import portfolio_financial_mvp_contract as financial

    content = contract_path.read_bytes()
    contract = json.loads(content)
    previous = json.loads(paths["contract"].read_bytes())
    assert contract["epoch_manifest_source"] == previous["epoch_manifest_source"]
    paths["contract"].write_bytes(content)
    policy = json.loads(paths["policy"].read_bytes())
    policy["product_authority"].update(
        raw_sha256=hashlib.sha256(content).hexdigest(),
        semantic_digest=financial.contract_digest(contract),
    )
    _write_json(paths["policy"], policy)
    calendar = json.loads(paths["calendar"].read_bytes())
    calendar["hwm_policy_sha256"] = hashlib.sha256(
        paths["policy"].read_bytes()
    ).hexdigest()
    calendar.pop("authority_evidence_sha256")
    calendar["authority_evidence_sha256"] = hashlib.sha256(
        json.dumps(calendar, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_json(paths["calendar"], calendar)
    receipt = json.loads(paths["receipt"].read_bytes())
    receipt.update(
        contract_raw_sha256=hashlib.sha256(content).hexdigest(),
        contract_semantic_sha256=financial.contract_digest(contract),
    )
    _write_json(paths["receipt"], receipt)


def build_hwm_vertical(root: Path, paths: dict[str, Path]) -> dict[str, Path]:
    """Execute the selected-source adapter and existing governed HWM kernel."""
    from quant_engine.portfolio_ledger.hwm_governed_execution import (
        compute_governed_as_held_composite_hwm,
    )

    hwm = _load(
        "mvp2_shared_hwm_fixture", "tests/portfolio_ledger/test_hwm_selected_inputs.py"
    )
    generation = hwm._build(paths)
    calendar = json.loads(paths["calendar"].read_bytes())
    calendar_root = calendar["authority_evidence_sha256"]
    start = min(
        date.fromisoformat(row["effective_start"]) for row in generation["memberships"]
    )
    complete = max(
        date.fromisoformat(row["interval_end"])
        for row in generation["component_periods"]
    )
    intervals = []
    for index in range((complete - start).days):
        left, right = start + timedelta(days=index), start + timedelta(days=index + 1)
        intervals.append(
            {
                "interval_id": f"selected-{index:04d}",
                "interval_start": left.isoformat(),
                "interval_end": right.isoformat(),
                "interval_evidence_sha256": hashlib.sha256(
                    f"{left.isoformat()}|{right.isoformat()}|{calendar_root}".encode()
                ).hexdigest(),
            }
        )
    input_path = _write_json(root / "hwm-input-generation.json", generation)
    result = compute_governed_as_held_composite_hwm(
        intervals,
        generation["memberships"],
        generation["component_periods"],
        policy_bytes=paths["policy"].read_bytes(),
        product_contract_bytes=paths["contract"].read_bytes(),
        performance_policy_bytes=(
            REPOSITORY_ROOT / "contracts/portfolio-performance-policy-v1.json"
        ).read_bytes(),
        legacy_policy_bytes=(
            REPOSITORY_ROOT / "contracts/portfolio-hwm-policy-v3.json"
        ).read_bytes(),
        calendar_authority_bytes=paths["calendar"].read_bytes(),
        input_generation_bytes=input_path.read_bytes(),
        input_receipt_bytes=paths["receipt"].read_bytes(),
        calendar_accepted_sha256=calendar_root,
        evaluated_on=(complete + timedelta(days=1)).isoformat(),
        complete_through=complete.isoformat(),
        maximum_age_days=30,
        assurance_profile="WORKSTATION_QC",
    )
    result_path = root / "as-held-hwm.json"
    result_path.write_bytes(
        (
            json.dumps(result, default=str, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
    )
    return {
        "governed_result": result_path,
        "input_generation": input_path,
        "calendar_authority": paths["calendar"],
        "provider_admission_receipt": paths["receipt"],
    }


def install_synthetic_financial_authority(
    monkeypatch: Any, contract_paths: list[Path]
) -> Any:
    """Configure only fixture source identities; retain canonical financial rules.

    Production admission is deliberately outside this numerical integration proof.
    Every financial producer and numeric replay verifier remains the real method.
    """
    import portfolio_financial_mvp_contract as financial
    import portfolio_invested_capital as capital
    import portfolio_current_dependence as current
    import portfolio_current_decomposition_plan as current_plan

    canonical = json.loads(
        (
            REPOSITORY_ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    accepted_paths = {path.resolve() for path in contract_paths}
    original = financial.load_contract

    def load_contract(
        contract_path: Path = financial.DEFAULT_CONTRACT, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        if Path(contract_path).resolve() not in accepted_paths:
            return original(contract_path, *args, **kwargs)
        contract = plan_tests.history_tests._synthetic_history_contract(
            Path(contract_path)
        )[0]
        assert contract["financial_outcomes"] == canonical["financial_outcomes"]
        # Only provider-generation source identities vary in the synthetic census.
        # Every membership, epoch, strategy and provider-lane rule stays canonical.
        from copy import deepcopy

        censuses = [
            deepcopy(value["current_census"]) for value in (contract, canonical)
        ]
        for census in censuses:
            for lane in census["provider_lanes"]:
                lane.pop("generation", None)
        assert censuses[0] == censuses[1]
        assert {
            row["strategy_id"]
            for row in contract["strategies"]
            if row["product_requirement"] == "REQUIRED"
        } == set(REQUIRED_STRATEGIES)
        registry_path = kwargs.get("registry_path") or (args[0] if args else None)
        if registry_path is not None:
            registry = next(
                row
                for row in contract["source_segments"]
                if row["source_id"] == "current-strategy-registry"
            )
            assert (
                hashlib.sha256(Path(registry_path).read_bytes()).hexdigest()
                == registry["current_generation"]["sha256"]
            )
        return contract

    # Bind the same explicit fixture authority to already imported real consumers.
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "load_contract", None) is original:
            monkeypatch.setattr(module, "load_contract", load_contract)
    monkeypatch.setattr(financial, "load_contract", load_contract)
    monkeypatch.setattr(capital, "load_contract", load_contract)

    def financial_module() -> Any:
        return financial

    monkeypatch.setattr(current, "_financial_module", financial_module)
    # The final numerical verifier imports the currently registered planner.
    # Collection can replace the planner held by plan_tests with another module
    # instance, so bind its history reader to these same explicit fixture paths.
    verifier = current_plan._layered_history_module()
    original_history_contract = verifier._history_contract

    def history_contract(path: Path, repository_sha: str | None = None) -> Any:
        if Path(path).resolve() not in accepted_paths:
            return original_history_contract(path, repository_sha)
        load_contract(path)
        return plan_tests.history_tests._synthetic_history_contract(path)

    monkeypatch.setattr(verifier, "_history_contract", history_contract)
    monkeypatch.setattr(current_plan, "_layered_history_module", lambda: verifier)
    return load_contract


def _dense_weekdays(start: date, end: date) -> list[date]:
    rows: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            rows.append(current)
        current += timedelta(days=1)
    return rows


def _write_market(path: Path, columns: tuple[str, ...], days: list[date]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("date", *columns))
        for index, observed in enumerate(days, 1):
            writer.writerow(
                (
                    observed.isoformat(),
                    *(
                        f"{(index + position) / 100000:.12f}"
                        for position in range(len(columns))
                    ),
                )
            )


def _align_live_source_dates(fixture: dict[str, Any], contract: dict[str, Any]) -> None:
    """Reissue synthetic live sources on one exact cross-strategy calendar."""

    history = plan_tests.history_tests
    minimum_training = 20
    minimum_holdout = 20
    holdout_fraction = 0.20
    observation_count = max(
        math.ceil(minimum_holdout / holdout_fraction),
        math.ceil(minimum_training / (1 - holdout_fraction)),
    )
    candidates = _dense_weekdays(date(2026, 1, 1), date(2026, 9, 4))
    common_days = candidates[-observation_count:]
    common_dates = tuple(item.isoformat() for item in common_days)
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["as_of_date"] = fixture["as_of_date"]
    templates = {row["strategy_id"]: row for row in catalog["strategy_returns"]}
    catalog["strategy_returns"] = [
        row
        for row in catalog["strategy_returns"]
        if row["strategy_id"] not in REQUIRED_STRATEGIES
    ] + [
        {
            **templates[strategy_id],
            "date": observed,
            "return": f"{index / 1000:.3f}",
        }
        for strategy_id in REQUIRED_STRATEGIES
        for index, observed in enumerate(common_dates, 1)
    ]
    for evidence in catalog["strategy_evidence"]:
        if evidence["strategy_id"] in REQUIRED_STRATEGIES:
            evidence["account"] = {
                "observed_end": common_dates[-1],
                "observed_start": common_dates[0],
                "return_observations": len(common_dates),
            }
    history._update_correction_interval(
        contract,
        "p123-eu-account-live",
        observed_start=common_dates[0],
        observed_end=common_dates[-1],
        effective_start=common_dates[0],
        effective_end=None,
    )

    matrix_nav = fixture["matrixport_nav_path"]
    first_matrix_day = common_days[0] - timedelta(days=5)
    all_matrix_days = [
        first_matrix_day + timedelta(days=index)
        for index in range((common_days[-1] - first_matrix_day).days + 1)
    ]
    pre_days = [item for item in all_matrix_days if item < common_days[0]]
    history._write_csv(
        matrix_nav,
        ["nav", "product_id", "timestamp_utc"],
        [
            {
                "nav": str(100 + index),
                "product_id": fixture["matrixport_product_id"],
                "timestamp_utc": f"{observed.isoformat()}T00:00:00Z",
            }
            for index, observed in enumerate(all_matrix_days)
        ],
    )
    matrix_daily = fixture["matrixport_daily_holdings_path"]
    history._write_csv(
        matrix_daily,
        ["product_id", "utc_date", "share", "nav", "nav_available"],
        [
            {
                "product_id": fixture["matrixport_product_id"],
                "utc_date": observed.isoformat(),
                "share": "10",
                "nav": str(100 + index),
                "nav_available": "true",
            }
            for index, observed in enumerate(all_matrix_days)
        ],
    )
    funding_day = (common_days[0] - timedelta(days=1)).isoformat()
    matrix_subscriptions = fixture["matrixport_subscription_path"]
    history._write_csv(
        matrix_subscriptions,
        [
            "confirm_nav_time_utc",
            "product_id",
            "status",
            "subscription_share",
            "subscription_time_utc",
        ],
        [
            {
                "confirm_nav_time_utc": f"{funding_day}T12:00:00Z",
                "product_id": fixture["matrixport_product_id"],
                "status": "300",
                "subscription_share": "10",
                "subscription_time_utc": f"{funding_day}T08:00:00Z",
            }
        ],
    )
    matrix_manifest_path = fixture["matrixport_manifest_path"]
    matrix_generation = history._core_generation_manifest(
        matrix_manifest_path,
        "quant-engine-matrixport",
        fixture["as_of_date"],
        [
            history._artifact(matrix_nav, "csv/nav_daily.csv"),
            history._artifact(fixture["matrixport_holdings_path"], "csv/holdings.csv"),
            history._artifact(matrix_daily, "csv/daily_holdings.csv"),
            history._artifact(matrix_subscriptions, "csv/subscription_details.csv"),
        ],
        {"declared_exclusions": []},
    )
    history._pointer(
        fixture["matrixport_pointer_path"], matrix_generation, matrix_manifest_path
    )
    history._update_source_generation(
        contract,
        "matrixport-account-generation",
        matrix_manifest_path,
        fixture["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "matrixport-account-generation",
        matrix_manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    matrix_source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    matrix_source["declared_gap_policy"] = history._matrixport_gap_policy(
        nav_path=matrix_nav,
        holdings_path=fixture["matrixport_holdings_path"],
        subscription_path=matrix_subscriptions,
        daily_holdings_path=matrix_daily,
        product_id=fixture["matrixport_product_id"],
    )
    matrix_returns, _matrix_coverage = history.target_series._matrixport_returns(
        matrix_nav,
        fixture["matrixport_holdings_path"],
        matrix_subscriptions,
        matrix_daily,
        fixture["matrixport_product_id"],
        REQUIRED_STRATEGIES["robuxio-crypto"],
        frozenset(),
    )
    catalog["strategy_returns"] = [
        row
        for row in catalog["strategy_returns"]
        if row["strategy_id"] != "robuxio-crypto"
    ] + [
        {
            **templates["robuxio-crypto"],
            "date": row["date"],
            "return": row["return"],
        }
        for row in matrix_returns
    ]
    robuxio_evidence = next(
        row
        for row in catalog["strategy_evidence"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    robuxio_evidence["account"] = {
        "observed_end": matrix_returns[-1]["date"],
        "observed_start": matrix_returns[0]["date"],
        "return_observations": len(matrix_returns),
    }
    _write_json(catalog_path, catalog)
    ledger_manifest_path = fixture["provider_ledger_manifest_path"]
    ledger_generation = history._core_generation_manifest(
        ledger_manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [history._artifact(catalog_path, "normalized/provider-ledger.json")],
    )
    history._pointer(
        fixture["provider_ledger_pointer_path"],
        ledger_generation,
        ledger_manifest_path,
    )
    history._update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    history._restore_retained_echo_correction(contract)
    history._update_correction_interval(
        contract,
        "robuxio-provider-live-before-funding",
        observed_start=pre_days[0].isoformat(),
        # The funding-day NAV anchors the account sleeve; it is not a
        # pre-funding provider return observation.
        observed_end=pre_days[-2].isoformat(),
        effective_start=pre_days[0].isoformat(),
        effective_end=funding_day,
    )
    history._update_correction_interval(
        contract,
        "robuxio-account-live-after-funding",
        observed_start=common_dates[0],
        observed_end=common_dates[-1],
        effective_start=funding_day,
        effective_end=None,
    )

    quantbase_history = fixture["quantbase_history_path"]
    history._write_csv(
        quantbase_history,
        ["time", "value", "cost_basis", "profit_loss"],
        [
            {
                "time": observed,
                "value": str(value),
                "cost_basis": "100",
                "profit_loss": str(value - 100),
            }
            for observed, value in [
                ((common_days[0] - timedelta(days=1)).strftime("%m/%d/%Y"), 100),
                *[
                    (observed.strftime("%m/%d/%Y"), 101 + index)
                    for index, observed in enumerate(common_days)
                ],
            ]
        ],
    )
    quantbase_activity = fixture["quantbase_activity_path"]
    history._write_csv(
        quantbase_activity,
        ["activity_type", "amount", "fund", "is_rebalance", "time", "type"],
        [
            {
                "activity_type": "filled_txn",
                "amount": "100",
                "fund": fixture["quantbase_fund_id"],
                "is_rebalance": "false",
                "time": f"{funding_day}T12:00:00Z",
                "type": "BUY",
            }
        ],
    )
    quantbase_manifest_path = fixture["quantbase_manifest_path"]
    quantbase_generation = history._core_generation_manifest(
        quantbase_manifest_path,
        "quant-engine-web-contract-harvester",
        fixture["as_of_date"],
        [
            history._artifact(
                quantbase_history,
                "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
            ),
            history._artifact(quantbase_activity, "source/csv/account_activity.csv"),
        ],
    )
    history._pointer(
        fixture["quantbase_pointer_path"],
        quantbase_generation,
        quantbase_manifest_path,
    )
    history._update_source_generation(
        contract,
        "quantbase-account-generation",
        quantbase_manifest_path,
        fixture["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "quantbase-account-generation",
        quantbase_manifest_path,
        fixture["as_of_date"],
        "QUANTBASE",
    )
    for correction_id in ("volatis-provider-live", "volatis-account-live"):
        history._update_correction_interval(
            contract,
            correction_id,
            observed_start=common_dates[0],
            observed_end=common_dates[-1],
            effective_start=funding_day,
            effective_end=None,
        )
    volatis_account = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "volatis-account-live"
    )
    volatis_account["funding_event"]["event_date"] = funding_day
    quantbase_returns, _quantbase_evidence = history.target_series._quantbase_returns(
        quantbase_history,
        quantbase_activity,
        fixture["quantbase_fund_id"],
        REQUIRED_STRATEGIES["volatis-strategy"],
    )
    catalog["strategy_returns"] = [
        row
        for row in catalog["strategy_returns"]
        if row["strategy_id"] != "volatis-strategy"
    ] + [
        {
            **templates["volatis-strategy"],
            "date": row["date"],
            "return": row["return"],
        }
        for row in quantbase_returns
    ]
    volatis_evidence = next(
        row
        for row in catalog["strategy_evidence"]
        if row["strategy_id"] == "volatis-strategy"
    )
    volatis_evidence["account"] = {
        "observed_end": quantbase_returns[-1]["date"],
        "observed_start": quantbase_returns[0]["date"],
        "return_observations": len(quantbase_returns),
    }
    _write_json(catalog_path, catalog)
    ledger_generation = history._core_generation_manifest(
        ledger_manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [history._artifact(catalog_path, "normalized/provider-ledger.json")],
    )
    history._pointer(
        fixture["provider_ledger_pointer_path"],
        ledger_generation,
        ledger_manifest_path,
    )
    history._update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    history._restore_retained_echo_correction(contract)


def _install_hybrid_source(
    fixture: dict[str, Any],
    *,
    live_starts: dict[str, str] | None = None,
    align_live_sources: bool = True,
) -> dict[str, Any]:
    """Add exact-version BACKTEST rows before the existing governed live rows."""

    history = plan_tests.history_tests
    fixture["as_of_date"] = "2026-09-04"
    returns_path = fixture["portfolio123_returns_path"]
    p123_rows = history.pq.read_table(returns_path).to_pylist()
    prehistory_start = date(2024, 1, 2)
    ai_live_start = date.fromisoformat(
        (live_starts or {}).get("p123-ai-factor", "2024-01-06")
    )
    p123_dates = [
        prehistory_start + timedelta(days=offset)
        for offset in range((ai_live_start - prehistory_start).days)
    ]
    if not p123_dates:
        raise AssertionError("synthetic AI prehistory must precede selected live start")
    p123_template = p123_rows[0]
    p123_rows = [
        {
            **p123_template,
            "model_return": float(100 + index / 100),
            "period_date": observed.isoformat(),
        }
        for index, observed in enumerate(p123_dates)
    ]
    history.pq.write_table(history.pa.Table.from_pylist(p123_rows), returns_path)
    p123_manifest_path = fixture["portfolio123_manifest_path"]
    p123_manifest = json.loads(p123_manifest_path.read_text(encoding="utf-8"))
    return_artifact = next(
        row
        for row in p123_manifest["artifacts"]
        if row["artifact_id"] == "tables/returns_daily.parquet"
    )
    return_artifact.update(
        history._artifact(returns_path, return_artifact["artifact_id"])
    )
    p123_generation = history._core_generation_manifest(
        p123_manifest_path,
        "quant-engine-portfolio123",
        fixture["as_of_date"],
        p123_manifest["artifacts"],
    )
    history._pointer(
        fixture["portfolio123_pointer_path"], p123_generation, p123_manifest_path
    )

    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    history._update_source_generation(
        contract,
        "portfolio123-model-generation",
        p123_manifest_path,
        fixture["as_of_date"],
    )
    history._update_correction_generations(
        contract,
        "portfolio123-model-generation",
        p123_manifest_path,
        fixture["as_of_date"],
        "PORTFOLIO123",
    )
    history._refresh_p123_eu_version(contract, p123_generation)
    history._append_p123_performance_receipt(
        contract, p123_manifest_path, fixture["portfolio123_performance_path"]
    )
    history._append_p123_ai_receipts(
        contract,
        p123_manifest_path,
        returns_path,
        fixture["portfolio123_relationships_path"],
    )
    if align_live_sources:
        _align_live_source_dates(fixture, contract)

    _targets, strategy_evidence, _proof, _ledger_manifest = (
        history.target_series.build_provider_ledger_targets(
            fixture["provider_ledger_manifest_path"],
            fixture["provider_ledger_catalog_path"],
        )
    )
    evidence = next(
        row
        for row in json.loads(strategy_evidence)["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    _ai_rows, ai_version, _ai_proof = history.target_series._p123_ai_backtest(
        evidence=evidence,
        returns_path=returns_path,
        relationships_path=fixture["portfolio123_relationships_path"],
        generation_id=p123_generation,
        relationships_sha256=hashlib.sha256(
            fixture["portfolio123_relationships_path"].read_bytes()
        ).hexdigest(),
        strategy_evidence_sha256=hashlib.sha256(strategy_evidence).hexdigest(),
        series_id=REQUIRED_STRATEGIES["p123-ai-factor"],
    )
    versions: dict[str, str] = {"p123-ai-factor": ai_version}
    for strategy in contract["strategies"]:
        strategy_id = strategy["strategy_id"]
        if strategy_id in REQUIRED_STRATEGIES:
            declared = versions.get(strategy_id) or strategy.get(
                "strategy_version", {}
            ).get("version_ref")
            version = (
                str(declared)
                if declared and "UNRESOLVED" not in str(declared)
                else f"synthetic-{strategy_id}:v1"
            )
            versions[strategy_id] = version
            strategy["strategy_version"] = {
                "status": "EVIDENCED",
                "version_ref": version,
            }

    rows = []
    external_strategies = {
        key: value
        for key, value in REQUIRED_STRATEGIES.items()
        if key != "p123-ai-factor"
    }
    external_start_dates = {
        strategy_id: date.fromisoformat(
            (live_starts or {}).get(strategy_id, "2024-01-04")
        )
        for strategy_id in external_strategies
    }
    for strategy_index, (strategy_id, series_id) in enumerate(
        external_strategies.items(), 1
    ):
        dates = [
            prehistory_start + timedelta(days=offset)
            for offset in range(
                (external_start_dates[strategy_id] - prehistory_start).days
            )
        ]
        if not dates:
            raise AssertionError(
                "synthetic prehistory must precede selected live start"
            )
        for day_index, observed in enumerate(dates, 1):
            rows.append(
                {
                    "date": observed.isoformat(),
                    "evidence_layer": "BACKTEST",
                    "return": f"{strategy_index * (1 + day_index % 11) / 1000000:.8f}",
                    "series_id": series_id,
                    "strategy_version": versions[strategy_id],
                }
            )
    returns_content = history._csv_bytes(
        history.target_series.CORRELATION_FIELDS_V2, rows
    )
    returns_path = fixture["contract_path"].with_name("strategy-history-returns.csv")
    returns_path.write_bytes(returns_content)
    manifest = {
        "as_of_date": fixture["as_of_date"],
        "builder": "portfolio-strategy-history",
        "builder_version": "1.4.0",
        "history_dispositions": [],
        "returns_artifact": {
            "artifact_id": "strategy-history/returns.csv",
            "sha256": hashlib.sha256(returns_content).hexdigest(),
            "size_bytes": len(returns_content),
        },
        "schema_version": 1,
        "series": [
            {
                "compatibility_basis": "SYNTHETIC_OWNER_ACCEPTED_VERSION",
                "frequency": "DAILY",
                "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                "implementation_reconciliation_status": "RECONCILED_SYNTHETIC",
                "observed_end": (
                    external_start_dates[strategy_id] - timedelta(days=1)
                ).isoformat(),
                "observed_start": prehistory_start.isoformat(),
                "required_for_analysis": True,
                "return_observations": (
                    external_start_dates[strategy_id] - prehistory_start
                ).days,
                "series_id": series_id,
                "strategy_version": versions[strategy_id],
            }
            for strategy_id, series_id in external_strategies.items()
        ],
        "verification": "VERIFIED",
    }
    manifest["bundle_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    manifest_path = _write_json(
        fixture["contract_path"].with_name("strategy-history-manifest.json"), manifest
    )
    fixture["strategy_history_manifest_path"] = manifest_path
    fixture["strategy_history_returns_path"] = returns_path

    backtest = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "strategy-backtests"
    )
    backtest.update(
        strategy_version={
            "binding": "EXACT_VERSION_COMPATIBILITY_REQUIRED",
            "status": "MIXED",
            "version_refs": versions,
        },
        current_generation={
            "as_of": fixture["as_of_date"],
            "generation_id": manifest["bundle_id"],
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        existence_status="EXISTS",
        capture_status="CAPTURED_GOVERNED",
    )
    backtest["interval"].update(
        observed_start=prehistory_start.isoformat(),
        observed_end=max(
            external_start - timedelta(days=1)
            for external_start in external_start_dates.values()
        ).isoformat(),
    )
    for source_id in (
        "portfolio123-model-generation",
        "quantbase-account-generation",
        "strategy-backtests",
    ):
        source = next(
            row for row in contract["source_segments"] if row["source_id"] == source_id
        )
        source["temporal"].update(calendar="ALWAYS_ON", cadence="DAILY")
    live = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "owner-account-live-history"
    )
    live["strategy_version"]["version_refs"].update(versions)
    for correction in contract["history_corrections"]["corrections"]:
        strategy_id = correction.get("strategy_id")
        if strategy_id in versions and isinstance(
            correction.get("strategy_version"), dict
        ):
            correction["strategy_version"]["version_ref"] = versions[strategy_id]
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        strategy_id = identity["strategy_id"]
        if strategy_id in versions:
            identity["strategy_version"] = versions[strategy_id]
    _write_json(fixture["contract_path"], contract)
    history._refresh_materialized_segment_identities(fixture)
    return fixture


def _complete_plan(plan: dict[str, Any]) -> None:
    plan["split_policy"].update(
        holdout_fraction="0.20",
        minimum_holdout_observations=20,
        minimum_training_observations_per_factor=20,
    )
    plan["sources"] = {
        source_key: {
            "lens": lens,
            "universe_id": universe,
            "input_key": input_key,
            "risk_free_column": "rf"
            if "economic" in source_key or source_key == "us-industry"
            else "BIL",
            "return_type": "EXCESS" if "economic" in source_key else "TOTAL",
            "column_policy": "LOWER_CANDIDATE_ID"
            if "economic" in source_key
            else "CANDIDATE_ID",
            **(
                {"freshness_policy_id": "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1"}
                if "economic" in source_key or source_key == "us-industry"
                else {"maximum_factor_staleness_days": 10}
            ),
        }
        for source_key, (lens, universe, input_key, _factor) in SOURCE_FACTORS.items()
    }
    candidates_by_universe: dict[str, set[str]] = {}
    for _source_key, (_lens, universe, _input_key, factor) in SOURCE_FACTORS.items():
        candidates_by_universe.setdefault(universe, set()).add(factor)
    for strategy in plan["strategies"]:
        if strategy["strategy_id"] not in REQUIRED_STRATEGIES:
            strategy.pop("inherit_assignments_from", None)
            strategy["assignments"] = []
            continue
        strategy.pop("inherit_assignments_from", None)
        strategy["assignments"] = [
            {
                "source_key": source_key,
                "constraint": "UNCONSTRAINED",
                "factor_sets": [[factor]],
                "screened_out_candidates": sorted(
                    candidates_by_universe[definition[1]] - {factor}
                ),
            }
            for source_key, (*definition, factor) in SOURCE_FACTORS.items()
        ]


@dataclass(frozen=True)
class DecompositionVertical:
    contract_path: Path
    layered_history_generation: Path
    plan_bundles: dict[str, Path]
    decomposition_generations: dict[tuple[str, str, str], Path]
    lenses_root: Path
    inputs: dict[str, Any]


def build_decomposition_vertical(
    root: Path,
    monkeypatch: Any,
    *,
    prepared_history_fixture: dict[str, Any] | None = None,
    before_models: Any = None,
) -> DecompositionVertical:
    """Run actual history, plan, and decomposition producers for all 24 surfaces."""

    if sys.platform == "win32" and not str(root).startswith("\\\\?\\"):
        root = Path("\\\\?\\" + str(root.resolve()))
    history = plan_tests.history_tests
    history._install_synthetic_history_authority(monkeypatch)
    verifier = plan_tests.planner._layered_history_module()
    history._install_synthetic_history_authority(monkeypatch, verifier)
    monkeypatch.setattr(plan_tests.planner, "_layered_history_module", lambda: verifier)

    original_history_fixture = history._history_fixture

    def hybrid_history_fixture(path: Path) -> dict[str, Any]:
        if prepared_history_fixture is not None:
            return prepared_history_fixture
        return _install_hybrid_source(original_history_fixture(path))

    monkeypatch.setattr(history, "_history_fixture", hybrid_history_fixture)
    inputs = plan_tests._fixture(root / "inputs", plan_mutator=_complete_plan)
    # This vertical expands the two-series planner fixture to all seven series.
    # Its synthetic factor calendar uses the same declared 252-anchor year.
    base_spec = json.loads(inputs["base_spec_path"].read_bytes())
    base_spec["models"] = [
        {"series_id": series_id, "periods_per_year": 252}
        for series_id in REQUIRED_STRATEGIES.values()
    ]
    _write_json(inputs["base_spec_path"], base_spec)

    candidates_by_universe: dict[str, set[str]] = {}
    for _key, (_lens, universe, _input, factor) in SOURCE_FACTORS.items():
        candidates_by_universe.setdefault(universe, set()).add(factor)
    catalog = {
        "universes": [
            {
                "universe_id": universe,
                "candidates": [
                    {"candidate_id": factor}
                    for factor in sorted(candidates_by_universe[universe])
                ],
            }
            for universe in sorted(candidates_by_universe)
        ]
    }
    _write_json(inputs["catalog_path"], catalog)
    days = _dense_weekdays(date(2024, 1, 2), date(2026, 9, 4))
    _write_market(inputs["market_paths"]["us_factors"], ("rf", "mkt_rf"), days)
    _write_market(inputs["market_paths"]["europe_factors"], ("rf", "mkt_rf"), days)
    _write_market(
        inputs["market_paths"]["style_returns"], ("BIL", "VALUE", "GROWTH"), days
    )
    _write_market(inputs["market_paths"]["sector_returns"], ("BIL", "TECH"), days)
    _write_market(inputs["market_paths"]["us_industries"], ("rf", "INDUSTRY"), days)
    catalog_sha = hashlib.sha256(inputs["catalog_path"].read_bytes()).hexdigest()
    evaluation = plan_tests._month_end(days[-1]) + timedelta(days=45)
    _write_json(
        inputs["public_factor_manifest_path"],
        plan_tests._public_factor_manifest(
            catalog_sha, inputs["market_paths"], evaluation
        ),
    )
    for kind in ("style", "sector"):
        source_path = inputs["market_paths"][f"{kind}_returns"]
        content = source_path.read_bytes()
        _write_json(
            inputs[f"{kind}_manifest_path"],
            {
                "catalog": {"sha256": catalog_sha},
                "output": {
                    "path": source_path.name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                },
            },
        )
    inputs["evaluation_date"] = evaluation.isoformat()

    plan_bundles: dict[str, Path] = {}
    generations: dict[tuple[str, str, str], Path] = {}
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    for mode_index, mode in enumerate(EVIDENCE_MODES):
        written = plan_tests.planner.write_bundle(
            output_root=root / "p" / str(mode_index),
            evidence_mode=mode,
            assurance_profile="DEVELOPMENT",
            **kwargs,
        )
        bundle = Path(written["destination"])
        plan_bundles[mode] = bundle
    vertical = DecompositionVertical(
        contract_path=inputs["financial_mvp_contract_path"],
        layered_history_generation=inputs["layered_history_generation_path"],
        plan_bundles=plan_bundles,
        decomposition_generations=generations,
        lenses_root=root / "d",
        inputs=inputs,
    )
    if before_models is not None:
        before_models(vertical)
    for mode, bundle in plan_bundles.items():
        manifest = json.loads(
            (bundle / "plan-manifest.json").read_text(encoding="utf-8")
        )
        for spec_index, spec_row in enumerate(manifest["specs"]):
            source_key = spec_row["source_key"]
            surface = spec_row["surface"]
            spec_path = bundle / spec_row["path"]
            output_root = root / "d" / mode / surface / source_key
            plan_tests.engine.stage_candidate(spec_path, bundle, output_root)
            published = plan_tests.engine.publish(spec_path, bundle, output_root)
            generations[(mode, surface, source_key)] = (
                output_root / "generations" / str(published["generation_id"])
            )

    return vertical


@dataclass(frozen=True)
class DependenceVertical:
    source_root: Path
    generation: Path
    policy_path: Path


def build_dependence_vertical(
    root: Path,
    decomposition: DecompositionVertical,
    baseline_generation: Path,
) -> DependenceVertical:
    """Use the selected actual layered history and baseline allocation bytes."""
    import portfolio_current_dependence as current
    import portfolio_correlation as correlation

    history = decomposition.layered_history_generation
    history_manifest = json.loads((history / "generation.json").read_bytes())
    baseline_manifest = json.loads(
        (baseline_generation / "generation.json").read_bytes()
    )
    baseline_path = baseline_generation / "portfolio-baseline.json"
    baseline = json.loads(baseline_path.read_bytes())
    policy = json.loads(
        (REPOSITORY_ROOT / "contracts/portfolio-dependence-policy-v1.json").read_bytes()
    )
    policy["history_authority"] = {
        "generation_id": history_manifest["generation_id"],
        "receipt_sha256": hashlib.sha256(
            (history / "generation.json").read_bytes()
        ).hexdigest(),
        "outputs": {
            name: {"sha256": row["sha256"], "size_bytes": row["size_bytes"]}
            for name, row in history_manifest["outputs"].items()
        },
    }
    policy["allocation_authority"] = {
        "generation_id": baseline_manifest["generation_id"],
        "manifest_sha256": hashlib.sha256(
            (baseline_generation / "generation.json").read_bytes()
        ).hexdigest(),
        "catalog_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "as_of_date": baseline_manifest["as_of_date"],
    }
    weights = {
        row["strategy_id"]: row["systematic_weight"]
        for row in baseline["allocations"]
        if row["scope"] == "SYSTEMATIC"
    }
    for row in policy["series"]:
        row["allocation_weight"] = weights[row["strategy_id"]]
    for row in policy["conceptual_priors"]:
        row["evidence_note"] = "Explicit synthetic conceptual prior; not measured data"
    policy_path = _write_json(root / "contracts/dependence-policy.json", policy)
    schema = root / "schemas/portfolio/dependence-policy.schema.json"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_bytes(
        (
            REPOSITORY_ROOT / "schemas/portfolio/dependence-policy.schema.json"
        ).read_bytes()
    )
    evaluated_on = max(
        date.fromisoformat(history_manifest["as_of_date"]),
        date.fromisoformat(baseline_manifest["as_of_date"]),
    )
    source_arguments = dict(
        contract_path=decomposition.contract_path,
        policy_path=policy_path,
        history_generation_root=history,
        allocation_generation_root=baseline_generation,
        evaluated_on=evaluated_on,
    )
    built = current.build_source(**source_arguments)
    # The retained workspace denies directory renames. Materialize the existing
    # producer's exact bundle directly, then use its independent rebuild check.
    source = root / "source" / built["receipt"]["bundle_id"]
    source.mkdir(parents=True, exist_ok=True)
    for name, content in built.items():
        if isinstance(content, bytes):
            destination = source / name
            if destination.exists():
                assert destination.read_bytes() == content
            else:
                destination.write_bytes(content)
    current.verify_source(source, **source_arguments)
    correlation.stage_candidate(source / "spec.json", source, root / "correlation")
    published = correlation.publish(source / "spec.json", source, root / "correlation")
    return DependenceVertical(
        source_root=source,
        generation=root / "correlation/generations" / str(published["generation_id"]),
        policy_path=policy_path,
    )


def decomposition_inventory(vertical: DecompositionVertical) -> tuple[Any, ...]:
    """Bind the finite 168 required cells to their 24 real producer containers."""
    from quant_engine.core.pipeline import ArtifactNode, GenerationNode, FileExpectation
    from portfolio_financial_outcomes import required_outcome_roles

    def artifact(
        node_id: str, path: Path, dependencies: tuple[str, ...] = ()
    ) -> ArtifactNode:
        content = path.read_bytes()
        return ArtifactNode(
            node_id,
            dependencies,
            path,
            hashlib.sha256(content).hexdigest(),
            len(content),
        )

    nodes: list[Any] = []
    for mode, bundle in vertical.plan_bundles.items():
        mode_id = mode.lower().replace("_", "-")
        manifest = json.loads((bundle / "plan-manifest.json").read_bytes())
        nodes.append(
            artifact(
                "financial-decomposition-plan-" + mode_id, bundle / "plan-manifest.json"
            )
        )
        for index, definition in enumerate([*manifest["inputs"], *manifest["outputs"]]):
            nodes.append(
                artifact(
                    f"financial-decomposition-source-{mode_id}-{index}",
                    bundle / definition["path"],
                )
            )
    nodes.append(
        artifact(
            "financial-layered-history-generation",
            vertical.layered_history_generation / "generation.json",
        )
    )
    generations: dict[tuple[str, str, str], str] = {}
    for index, (key, path) in enumerate(
        sorted(vertical.decomposition_generations.items())
    ):
        manifest_path = path / "generation.json"
        manifest_content = manifest_path.read_bytes()
        manifest = json.loads(manifest_content)
        node_id = f"financial-decomposition-generation-{index}"
        generations[key] = node_id
        files = tuple(
            FileExpectation(
                child.relative_to(path).as_posix(),
                hashlib.sha256(child.read_bytes()).hexdigest(),
                child.stat().st_size,
            )
            for child in sorted(path.rglob("*"))
            if child.is_file()
        )
        nodes.append(
            GenerationNode(
                node_id,
                (),
                f"decomposition-{index}",
                manifest["generation_id"],
                hashlib.sha256(manifest_content).hexdigest(),
                files,
                path,
                False,
            )
        )
    contract = json.loads(vertical.contract_path.read_bytes())
    plan = json.loads(
        (
            vertical.plan_bundles["LIVE_ONLY"]
            / "inputs/governance/current-strategy-decomposition-plan.json"
        ).read_bytes()
    )
    for role, selection in required_outcome_roles(contract, plan=plan).items():
        if selection[0] != "DECOMPOSITION":
            continue
        _, _strategy, source, _lens, surface, mode = selection
        key = (mode, surface, source)
        nodes.append(
            artifact(
                role,
                vertical.decomposition_generations[key] / "decomposition.json",
                (generations[key],),
            )
        )
    return tuple(nodes)
