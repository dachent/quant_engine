"""The report refuses an implementation contract whose account leg is not the
governed account-live series (QC-1 N66; closure-matrix B2).

The numbers below keep the shape of the real pair that QC-1 found: one ledger
vintage carried -3.3213% for the last day of the Grizzly window and the governed
vintage carried -3.0924%, so two account returns existed for one window.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS_ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load("portfolio_report_account_leg_test", "portfolio_report.py")
reconciliation = _load(
    "portfolio_implementation_reconciliation",
    "portfolio_implementation_reconciliation.py",
)
attribution = _load(
    "portfolio_implementation_return_attribution",
    "portfolio_implementation_return_attribution.py",
)

STRATEGIES = ("grizzlybears-vtma", "p123-ai-factor", "tt-portfolio")
AS_OF = date(2026, 8, 3)
# Two ledger vintages of one Schwab account: identical but for the last day.
DATES = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")
OLD_VINTAGE = (
    "-0.0028962",
    "0.0125976854165033",
    "-0.0083439556684643",
    "-0.0332133263079033",
)
GOVERNED = (
    "-0.0028962",
    "0.012597685416503276790239893",
    "-0.0083439556684642926119059292",
    "-0.0309240051291309354865271400",
)


def _ledger_rows(strategy_id: str, values: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {
            "date": observed,
            "evidence_layer": "ACCOUNT_LIVE",
            "return": value,
            "strategy_id": strategy_id,
        }
        for observed, value in zip(DATES, values, strict=True)
    ]


def _governed(values: tuple[str, ...] = GOVERNED) -> dict[str, dict[str, str]]:
    return {
        strategy_id: {
            row["date"]: row["return"] for row in _ledger_rows(strategy_id, values)
        }
        for strategy_id in STRATEGIES
    }


def _compound(values: tuple[str, ...]) -> float:
    result = Decimal(1)
    for value in values:
        result *= 1 + Decimal(value)
    return float(result - 1)


def _reconciliation_row(strategy_id: str, values: tuple[str, ...]) -> dict[str, Any]:
    row = {
        "return_reconciliation": {
            "account_cumulative_return": _compound(values),
            "account_minus_model_cumulative": -0.1,
            "model_cumulative_return": _compound(values) + 0.1,
            "observations": len(values),
            "observed_end": DATES[-1],
            "observed_start": DATES[0],
            "status": "TESTED_EXACT_COMMON_DATES",
        },
        "root_causes": [
            {"cause": "SYNTHETIC", "evidence": "test", "status": "CONFIRMED"}
        ],
        "strategy_id": strategy_id,
        "verification": "VERIFIED",
    }
    return reconciliation.with_account_leg(
        row,
        _ledger_rows(strategy_id, values),
        strategy_id=strategy_id,
        accepted_as_of=AS_OF,
    )


def _write_reconciliation(
    path: Path,
    values: tuple[str, ...] = GOVERNED,
    *,
    schema_version: int = 2,
    strip_leg: bool = False,
    mutate: Any = None,
) -> None:
    strategies = []
    for strategy_id in STRATEGIES:
        row = _reconciliation_row(strategy_id, values)
        if strip_leg or schema_version == 1:
            row.pop("account_leg")
            for key in ("account_basis", "model_basis"):
                row["return_reconciliation"].pop(key, None)
        strategies.append(row)
    payload: dict[str, Any] = {
        "as_of_date": AS_OF.isoformat(),
        "builder": "portfolio-implementation-reconciliation",
        "reconciliation_id": "a" * 64,
        "schema_version": schema_version,
        "strategies": strategies,
        "verification": "VERIFIED",
    }
    if mutate is not None:
        mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _attribution_row(strategy_id: str, values: tuple[str, ...]) -> dict[str, Any]:
    account = _compound(values)
    row = {
        "bridge": {
            "account_return": account,
            "categories": [
                {"category": "RESIDUAL", "effect_pp": (account - account - 0.1) * 100.0}
            ],
            "gap_pp": -10.0,
            "model_return": account + 0.1,
            "status": "EXACT_OPERATIONAL_BRIDGE_NO_UNNAMED_RESIDUAL",
        },
        "observed_end": DATES[-1],
        "observed_start": DATES[0],
        "strategy_id": strategy_id,
        "verification": "VERIFIED_WITH_DECLARED_PROXY",
    }
    return attribution.with_account_leg(
        row,
        _ledger_rows(strategy_id, values),
        strategy_id=strategy_id,
        accepted_as_of=AS_OF,
        model_basis="EXACT_MODEL_RETURNS",
    )


def _write_attribution(
    path: Path,
    values: tuple[str, ...] = GOVERNED,
    *,
    schema_version: int = 2,
    strip_leg: bool = False,
) -> None:
    strategies = []
    for strategy_id in STRATEGIES:
        row = _attribution_row(strategy_id, values)
        if strip_leg or schema_version == 1:
            row.pop("account_leg")
            for key in ("account_basis", "model_basis"):
                row["bridge"].pop(key, None)
        strategies.append(row)
    path.write_text(
        json.dumps(
            {
                "as_of_date": AS_OF.isoformat(),
                "attribution_id": "b" * 64,
                "builder": "portfolio-implementation-return-attribution",
                "schema_version": schema_version,
                "strategies": strategies,
                "verification": "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES",
            }
        ),
        encoding="utf-8",
    )


def _reconcile(path: Path, governed: Any) -> tuple[Any, Any]:
    return reporter._implementation_reconciliation_contract(
        path, portfolio_as_of="2026-09-17", governed_account_returns=governed
    )


def _attribute(path: Path, governed: Any) -> tuple[Any, Any]:
    return reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-09-17", governed_account_returns=governed
    )


def test_reconciliation_bound_to_the_governed_series_is_accepted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path)

    rows, ref = _reconcile(path, _governed())

    assert set(rows) == set(STRATEGIES)
    assert ref["schema_version"] == 2
    grizzly = rows["grizzlybears-vtma"]
    assert grizzly["account_leg"]["basis"] == "BASELINE_ACCOUNT_LIVE"
    assert grizzly["return_reconciliation"]["account_cumulative_return"] == (
        pytest.approx(_compound(GOVERNED), abs=1e-12)
    )


def test_reconciliation_built_from_an_older_ledger_vintage_is_refused(
    tmp_path: Path,
) -> None:
    # The QC-1 N66 pair: the contract was built from the old vintage while the
    # governed account-live series carries the revised last day.
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path, OLD_VINTAGE)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: grizzlybears-vtma",
    ):
        _reconcile(path, _governed())

    assert _compound(OLD_VINTAGE) != pytest.approx(_compound(GOVERNED), abs=1e-4)


def test_reconciliation_leg_that_differs_only_by_representation_is_accepted(
    tmp_path: Path,
) -> None:
    # A float-repr ledger and a 28-digit decimal ledger of one series carry
    # different bytes (so a different digest) and the same values.
    path = tmp_path / "reconciliation.json"
    old_repr = tuple(
        repr(float(value)) if index < 3 else value
        for index, value in enumerate(GOVERNED)
    )
    _write_reconciliation(path, old_repr)

    rows, _ = _reconcile(path, _governed())

    governed_digest = reconciliation.account_series_sha256(
        [
            {"date": observed, "return": value}
            for observed, value in _governed()["tt-portfolio"].items()
        ]
    )
    leg = rows["tt-portfolio"]["account_leg"]
    assert leg["series_sha256"] != governed_digest
    assert leg["cumulative_return"] == pytest.approx(_compound(GOVERNED), abs=1e-9)


def test_reconciliation_v2_without_a_leg_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path, schema_version=2, strip_leg=True)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg is absent: grizzlybears-vtma",
    ):
        _reconcile(path, _governed())


def test_reconciliation_whose_published_return_is_not_its_leg_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"

    def tamper(payload: dict[str, Any]) -> None:
        payload["strategies"][0]["return_reconciliation"][
            "account_cumulative_return"
        ] += 0.0023

    _write_reconciliation(path, mutate=tamper)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation published account return differs from its leg: "
        "grizzlybears-vtma",
    ):
        _reconcile(path, _governed())


def test_reconciliation_leg_over_dates_the_governed_series_lacks_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path)
    governed = _governed()
    del governed["tt-portfolio"]["2026-01-06"]

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: tt-portfolio",
    ):
        _reconcile(path, governed)


def test_legacy_v1_reconciliation_that_equals_the_governed_window_is_accepted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path, schema_version=1)

    rows, ref = _reconcile(path, _governed())

    assert ref["schema_version"] == 1
    assert "account_leg" not in rows["grizzlybears-vtma"]


def test_legacy_v1_reconciliation_off_the_governed_window_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path, OLD_VINTAGE, schema_version=1)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: grizzlybears-vtma",
    ):
        _reconcile(path, _governed())


def test_reconciliation_without_a_governed_series_is_structural_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"
    _write_reconciliation(path, OLD_VINTAGE)

    rows, _ = _reconcile(path, None)

    assert set(rows) == set(STRATEGIES)


def test_attribution_bound_to_the_governed_series_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "attribution.json"
    _write_attribution(path)

    rows, ref = _attribute(path, _governed())

    assert ref["schema_version"] == 2
    assert rows["tt-portfolio"]["bridge"]["account_basis"] == "BASELINE_ACCOUNT_LIVE"
    assert rows["tt-portfolio"]["bridge"]["model_basis"] == "EXACT_MODEL_RETURNS"


def test_attribution_built_from_an_older_ledger_vintage_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attribution.json"
    _write_attribution(path, OLD_VINTAGE)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: grizzlybears-vtma",
    ):
        _attribute(path, _governed())


def test_attribution_v2_without_a_leg_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "attribution.json"
    _write_attribution(path, schema_version=2, strip_leg=True)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg is absent: grizzlybears-vtma",
    ):
        _attribute(path, _governed())


def test_legacy_v1_attribution_is_checked_over_its_declared_window(
    tmp_path: Path,
) -> None:
    accepted = tmp_path / "accepted.json"
    refused = tmp_path / "refused.json"
    _write_attribution(accepted, schema_version=1)
    _write_attribution(refused, OLD_VINTAGE, schema_version=1)

    rows, _ = _attribute(accepted, _governed())
    assert set(rows) == set(STRATEGIES)
    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series",
    ):
        _attribute(refused, _governed())


def test_governed_account_returns_are_read_from_the_catalog_rows() -> None:
    catalog = {
        "strategy_returns": [
            {
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "0.0100",
                "strategy_id": "qsi",
            },
            {
                "date": "2026-01-05",
                "evidence_layer": "PROVIDER_LIVE",
                "return": "0.5",
                "strategy_id": "qsi",
            },
            {
                "date": "2026-01-06",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "-0.0",
                "strategy_id": "qsi",
            },
        ]
    }

    assert reporter._governed_account_returns(catalog) == {
        "qsi": {"2026-01-05": "0.01", "2026-01-06": "0"}
    }


def test_reconciliation_leg_window_count_must_match_the_governed_dates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliation.json"

    def tamper(payload: dict[str, Any]) -> None:
        payload["strategies"][2]["account_leg"]["window_observations"] += 1

    _write_reconciliation(path, mutate=tamper)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: tt-portfolio",
    ):
        _reconcile(path, _governed())


def test_reconciliation_leg_with_an_unknown_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "reconciliation.json"

    def tamper(payload: dict[str, Any]) -> None:
        payload["strategies"][0]["account_leg"]["note"] = "x"

    _write_reconciliation(path, mutate=tamper)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from its contract: grizzlybears-vtma",
    ):
        _reconcile(path, _governed())


def _catalog(values: tuple[str, ...] = GOVERNED) -> dict[str, Any]:
    return {
        "as_of_date": "2026-09-17",
        "strategy_returns": [
            row
            for strategy_id in STRATEGIES
            for row in _ledger_rows(strategy_id, values)
        ],
    }


def test_report_build_reads_both_contracts_against_the_catalog_series(
    tmp_path: Path,
) -> None:
    reconciliation_path = tmp_path / "reconciliation.json"
    attribution_path = tmp_path / "attribution.json"
    _write_reconciliation(reconciliation_path)
    _write_attribution(attribution_path)

    rows, ref, attribution_rows, attribution_ref = reporter._implementation_contracts(
        _catalog(), tmp_path, None, reconciliation_path, attribution_path
    )

    assert rows is not None and attribution_rows is not None
    assert ref is not None and attribution_ref is not None
    assert ref["schema_version"] == attribution_ref["schema_version"] == 2
    assert reporter._implementation_contracts(
        _catalog(), tmp_path, None, None, None
    ) == (
        None,
        None,
        None,
        None,
    )


def test_report_build_refuses_contracts_built_from_another_ledger_vintage(
    tmp_path: Path,
) -> None:
    stale_reconciliation = tmp_path / "stale-reconciliation.json"
    stale_attribution = tmp_path / "stale-attribution.json"
    _write_reconciliation(stale_reconciliation, OLD_VINTAGE)
    _write_attribution(stale_attribution, OLD_VINTAGE)

    for reconciliation_path, attribution_path in (
        (stale_reconciliation, None),
        (None, stale_attribution),
    ):
        with pytest.raises(
            reporter.ReportValidationError,
            match="implementation account leg differs from the governed account-live "
            "series: grizzlybears-vtma",
        ):
            reporter._implementation_contracts(
                _catalog(), tmp_path, None, reconciliation_path, attribution_path
            )
