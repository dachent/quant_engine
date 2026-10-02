"""Build exact-common-date strategy attribution without inventing missing evidence."""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    read_current,
    read_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import ReturnSpan, ReturnsError, splice_spans
from portfolio_current_dependence import (
    verify_packaged_source as _verify_dependence_source,
)
from quant_engine.portfolio_ledger.value_basis_bounds import (
    derive_value_basis_bounds,
    ValueBasisBoundError,
)
from portfolio_report import (
    ReportValidationError,
    _account_live_return_series as _validated_account_live_return_series,
    _epoch_hpr_receipts as _validated_epoch_hpr_receipts,
)


SCHEMA_VERSION: Final = 2
CATALOG_SCHEMA_VERSION: Final = 2
BUILDER_VERSION: Final = "1.2.1"
STRATEGY_COUNT: Final = 9
CATALOG_NAME: Final = "strategy-attribution.json"
AVAILABLE_STATUSES: Final = {
    "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
    "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
}
AVAILABLE_STATUS: Final = "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"
NOT_READY_STATUS: Final = "NOT_READY"
# A comparison declaring fewer periods a year than this has a reference coarser
# than daily (weekly, monthly): its account side is compounded per reference period.
DAILY_PERIODS_PER_YEAR: Final = 252
# Where the account side of an available comparison was read from.
ACCOUNT_SOURCE_BASELINE_LIVE: Final = "BASELINE_ACCOUNT_LIVE"
ACCOUNT_SOURCE_RETAINED_HISTORY: Final = "RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY"


class AttributionValidationError(ValueError):
    """Raised when attribution inputs or identities are unsafe or ambiguous."""


def _epoch_receipts(
    baseline: Mapping[str, Any],
    baseline_path: Path,
    source_root: Path | None,
    selected_path: Path | None,
) -> tuple[dict[str, dict[str, Any]], list[SourceArtifact]]:
    """Admit receipt evidence from the existing selected dependence package only."""
    if (source_root is None) != (selected_path is None):
        raise AttributionValidationError(
            "dependence source and selected inputs must be paired"
        )
    if source_root is None:
        if baseline.get("epoch_hpr_receipts") or any(
            row.get("provider_id") == "matrixport"
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
            for row in baseline.get("strategy_returns", [])
        ):
            raise AttributionValidationError(
                "receipt-capable account requires selected dependence authority"
            )
        return {}, []
    assert selected_path is not None
    selected = _load_json(selected_path, "selected dependence inputs")
    allocation = Path(
        _text(
            selected.get("allocation_generation_root"), "selected allocation generation"
        )
    )
    if allocation.resolve() != baseline_path.parent.resolve():
        raise AttributionValidationError(
            "selected allocation generation differs from attribution baseline"
        )
    contract = Path(
        _text(
            selected.get("financial_input_contract_path"), "selected financial contract"
        )
    )
    policy = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "portfolio-dependence-policy-v1.json"
    )
    ledger_root = Path(
        _text(
            selected.get("provider_ledger_generation_root"), "selected provider ledger"
        )
    )
    sources = [
        ("dependence-selected-inputs.json", selected_path),
        ("dependence-source.json", source_root / "dependence-source.json"),
        ("dependence-history-coverage.json", source_root / "history-coverage.json"),
        ("dependence-financial-contract.json", contract),
        ("dependence-policy.json", policy),
        ("dependence-provider-ledger.json", ledger_root / "provider-ledger.json"),
    ]
    identities = [(name, path, *_hash(path, name)) for name, path in sources]
    try:
        verified = _verify_dependence_source(
            source_root,
            contract_path=contract,
            policy_path=policy,
            selected_inputs_path=selected_path,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise AttributionValidationError(
            f"selected dependence authority is invalid: {exc}"
        ) from exc
    if verified.get("verification") != "VALID" or not isinstance(
        verified.get("epoch_hpr_receipts"), list
    ):
        raise AttributionValidationError("selected dependence verification differs")
    for name, path, digest, size in identities:
        if _hash(path, name) != (digest, size):
            raise AttributionValidationError(
                "selected dependence authority changed during verification"
            )
    ledger = _load_json(
        ledger_root / "provider-ledger.json", "selected provider ledger"
    )
    for outer in verified["epoch_hpr_receipts"]:
        strategy_id = outer.get("strategy_id")
        if not any(
            row.get("provider_id") == "matrixport"
            and row.get("generation_id") == outer.get("source_generation_id")
            for row in ledger.get("provider_generations", [])
        ):
            raise AttributionValidationError(
                "epoch HPR receipt authority differs from selected provider generation"
            )

        def bound_rows(
            catalog: Mapping[str, Any],
        ) -> list[tuple[Any, Any, tuple[Any, ...], Any]]:
            return sorted(
                (
                    row.get("date"),
                    row.get("return"),
                    tuple(row.get("component_account_ids", [])),
                    row.get("account_reconciliation_sha256"),
                )
                for row in catalog.get("strategy_returns", [])
                if row.get("strategy_id") == strategy_id
                and row.get("evidence_layer") == "ACCOUNT_LIVE"
            )

        observed = bound_rows(baseline)
        if (
            not observed
            or observed != bound_rows(ledger)
            or any(len(row[2]) != 1 for row in observed)
        ):
            raise AttributionValidationError(
                "epoch HPR account identity differs from selected ledger"
            )
    transient = {**baseline, "epoch_hpr_receipts": verified["epoch_hpr_receipts"]}
    try:
        bounds = None
        if any(
            outer.get("receipt", {}).get("admission_cutoff")
            != outer.get("receipt", {}).get("observed_end")
            for outer in verified["epoch_hpr_receipts"]
        ):
            bounds = derive_value_basis_bounds(
                ledger,
                _load_json(contract, "selected financial contract"),
                date.fromisoformat(str(baseline["as_of_date"])),
            )
        receipts = _validated_epoch_hpr_receipts(
            transient, calculation_cutoff_date=None, value_basis_bounds=bounds
        )
        _validated_account_live_return_series(transient, value_basis_bounds=bounds)
    except (ReportValidationError, ValueBasisBoundError) as exc:
        raise AttributionValidationError(str(exc)) from exc
    as_of = date.fromisoformat(str(baseline["as_of_date"]))
    return receipts, [
        SourceArtifact(
            artifact_id=f"input/{name}",
            sha256=digest,
            size_bytes=size,
            as_of_date=as_of,
            schema_name="selected-dependence-authority",
            schema_version="1",
        )
        for name, _, digest, size in identities
    ]


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttributionValidationError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AttributionValidationError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AttributionValidationError(f"{name} must be non-empty text")
    return value


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AttributionValidationError(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise AttributionValidationError(f"{name} must be finite")
    return parsed


def _decimal_text(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return format(value, ".15g")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _hash(path: Path, name: str) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise AttributionValidationError(f"{name} is missing or linked")
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _verify_hash(path: Path, expected: object, name: str) -> tuple[str, int]:
    expected_hash = _text(expected, f"{name}.expected_sha256")
    actual, size = _hash(path, name)
    if actual != expected_hash:
        raise AttributionValidationError(f"{name} hash differs")
    return actual, size


def _baseline(
    root: Path, baseline_generation_id: str | None = None
) -> tuple[dict[str, Any], dict[str, str], Path, SourceArtifact]:
    if baseline_generation_id is None:
        pointer = read_current(root)
        baseline_generation_id = pointer.generation_id
    if re.fullmatch(r"[0-9a-f]{64}", baseline_generation_id) is None:
        raise AttributionValidationError("baseline generation identity differs")
    try:
        manifest = read_generation(root, baseline_generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise AttributionValidationError("baseline generation is not accepted") from exc
    generation = root / "generations" / baseline_generation_id
    manifest_path = generation / "generation.json"
    catalog_path = generation / "portfolio-baseline.json"
    expected_manifest = manifest.to_json_bytes()
    manifest_sha256, manifest_size_bytes = _hash(manifest_path, "baseline manifest")
    if (
        manifest.generation_id != baseline_generation_id
        or manifest.verification is not VerificationStatus.VERIFIED
        or manifest_sha256 != hashlib.sha256(expected_manifest).hexdigest()
        or manifest_size_bytes != len(expected_manifest)
    ):
        raise AttributionValidationError("baseline generation contract differs")
    if manifest.as_of_date != max(item.as_of_date for item in manifest.artifacts):
        raise AttributionValidationError(
            "baseline generation date is not artifact-derived"
        )
    catalog = _load_json(catalog_path, "baseline catalog")
    if catalog.get("baseline_schema_version") != 1:
        raise AttributionValidationError("baseline schema is unsupported")
    try:
        catalog_as_of = date.fromisoformat(
            _text(catalog.get("as_of_date"), "baseline.as_of_date")
        )
    except ValueError as exc:
        raise AttributionValidationError(
            "baseline catalog as-of date is invalid"
        ) from exc
    catalog_sha256, catalog_size_bytes = _hash(catalog_path, "baseline catalog")
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/portfolio-baseline.json"
        ),
        None,
    )
    if artifact is None or (
        artifact.sha256,
        artifact.size_bytes,
        artifact.as_of_date,
        artifact.schema_name,
        artifact.schema_version,
    ) != (
        catalog_sha256,
        catalog_size_bytes,
        catalog_as_of,
        "portfolio-baseline-catalog",
        "1",
    ):
        raise AttributionValidationError("baseline artifact identity differs")
    return (
        catalog,
        {
            "generation_id": baseline_generation_id,
            "catalog_sha256": catalog_sha256,
        },
        catalog_path,
        SourceArtifact(
            artifact_id="input/portfolio-baseline-generation.json",
            sha256=manifest_sha256,
            size_bytes=manifest_size_bytes,
            as_of_date=manifest.as_of_date,
            schema_name="generation-manifest",
            schema_version="1",
        ),
    )


def _rows(
    path: Path,
) -> tuple[dict[str, dict[date, Decimal]], dict[str, tuple[str, str, str]]]:
    """Series values and each series' (evidence_layer, version_ref, source)."""

    result: dict[str, dict[date, Decimal]] = {}
    identities: dict[str, tuple[str, str, str]] = {}
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != [
                "series_id",
                "date",
                "return",
                "evidence_layer",
                "version_ref",
                "source_artifact_id",
            ]:
                raise AttributionValidationError("return source columns differ")
            for row in reader:
                series_id = _text(row.get("series_id"), "return.series_id")
                day = date.fromisoformat(_text(row.get("date"), "return.date"))
                value = _decimal(row.get("return"), "return.return")
                if value <= -1:
                    raise AttributionValidationError("return must be greater than -1")
                identity = (
                    _text(row.get("evidence_layer"), "return.evidence_layer"),
                    _text(row.get("version_ref"), "return.version_ref"),
                    _text(row.get("source_artifact_id"), "return.source_artifact_id"),
                )
                if series_id in identities and identities[series_id] != identity:
                    raise AttributionValidationError(
                        "series identity changes within source"
                    )
                identities[series_id] = identity
                series = result.setdefault(series_id, {})
                if day in series and series[day] != value:
                    raise AttributionValidationError("series has conflicting dates")
                series[day] = value
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, AttributionValidationError):
            raise
        raise AttributionValidationError("return source is unreadable") from exc
    return result, identities


def _account_live_series(
    baseline: Mapping[str, Any],
) -> dict[str, dict[date, Decimal]]:
    """The baseline's own admitted ACCOUNT_LIVE daily return per strategy.

    This is the account side of a comparison (B3): the baseline already
    carries the provider-ledger's admitted, authoritative strategy return
    series (``_baseline()`` loads the whole catalog), so attribution reads it
    directly instead of a separately retained CSV that can fall behind.
    """

    rows = baseline.get("strategy_returns")
    result: dict[str, dict[date, Decimal]] = {}
    if rows is None:
        return result
    if not isinstance(rows, list):
        raise AttributionValidationError("baseline strategy_returns must be an array")
    for row in rows:
        if not isinstance(row, dict) or row.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy_id = _text(row.get("strategy_id"), "strategy_returns.strategy_id")
        day = date.fromisoformat(_text(row.get("date"), "strategy_returns.date"))
        value = _decimal(row.get("return"), "strategy_returns.return")
        if value <= -1:
            raise AttributionValidationError(
                "strategy_returns.return must be greater than -1"
            )
        series = result.setdefault(strategy_id, {})
        if day in series and series[day] != value:
            raise AttributionValidationError(
                "baseline strategy_returns has conflicting dates"
            )
        series[day] = value
    return result


def _reference_only_account_history(
    baseline: Mapping[str, Any],
) -> dict[str, str]:
    """Strategies whose account-live returns the baseline keeps as history only.

    Maps strategy_id to the version_ref its registry record names, for each
    strategy whose baseline ``provider_admissions`` rows for the
    ``strategy_returns`` ACCOUNT_LIVE fact exist and are all REFERENCE_ONLY.
    The baseline then deliberately carries no live series for it (a retired,
    history-only strategy such as EchoTrade), unlike an AUTHORITATIVE
    admission whose rows are missing, which must fail closed.
    """

    admissions = baseline.get("provider_admissions")
    dispositions: dict[str, set[object]] = {}
    for row in admissions if isinstance(admissions, list) else []:
        if (
            isinstance(row, dict)
            and row.get("fact_type") == "strategy_returns"
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
            and isinstance(row.get("strategy_id"), str)
        ):
            dispositions.setdefault(row["strategy_id"], set()).add(
                row.get("disposition")
            )
    result: dict[str, str] = {}
    for item in baseline.get("strategies", []):
        if not isinstance(item, dict):
            continue
        strategy_id = item.get("strategy_id")
        version_ref = item.get("version_ref")
        if (
            isinstance(strategy_id, str)
            and dispositions.get(strategy_id) == {"REFERENCE_ONLY"}
            and isinstance(version_ref, str)
            and version_ref
        ):
            result[strategy_id] = version_ref
    return result


def _retained_account_history(
    strategy_id: str,
    account_series_id: str,
    reference_only_history: Mapping[str, str],
    retained_series: Mapping[str, Mapping[date, Decimal]],
    retained_identities: Mapping[str, tuple[str, str, str]],
) -> Mapping[date, Decimal]:
    """The account side of a strategy the baseline keeps as history only.

    Read only when the baseline has no ACCOUNT_LIVE ``strategy_returns`` row
    for the strategy. The strategy must be history-only (every account-live
    admission REFERENCE_ONLY) and the retained series named by
    ``account_series_id`` must be the ACCOUNT_LIVE path of exactly the version
    the baseline registers for it. Anything else fails closed, so a strategy
    that lost its admitted live rows never falls back to the retained file.
    """

    version_ref = reference_only_history.get(strategy_id)
    if version_ref is None or account_series_id not in retained_series:
        raise AttributionValidationError("comparison series is missing")
    evidence_layer, series_version_ref, _ = retained_identities[account_series_id]
    if evidence_layer != "ACCOUNT_LIVE" or series_version_ref != version_ref:
        raise AttributionValidationError("retained account history identity differs")
    return retained_series[account_series_id]


def _compound_to_reference_periods(
    account: Mapping[date, Decimal],
    reference: Mapping[date, Decimal],
    window: Mapping[date, Decimal],
) -> dict[date, Decimal]:
    """The account's returns compounded over each coarse reference period.

    A reference return dated ``day`` covers the period from the previous date of
    the whole reference series (not only of the comparison window) up to and
    including ``day``. Pairing it with the account's one-day return on ``day``
    would compare two horizons, so the account's own returns inside that period
    are compounded into one return dated ``day``. The first date of the series has
    no previous date; its period is taken to be as long as the next one. A period
    without an account return yields no pair, and account returns outside every
    period of the window are not used.
    """

    dates = sorted(reference)
    opens = dict(zip(dates[1:], dates))
    if len(dates) > 1:
        opens[dates[0]] = dates[0] - (dates[1] - dates[0])
    account_dates = sorted(account)
    result: dict[date, Decimal] = {}
    for day in sorted(window):
        if day not in opens:  # a one-date series has no period to compound over
            continue
        inside = [
            (item, account[item]) for item in account_dates if opens[day] < item <= day
        ]
        if inside:
            result[day] = splice_spans(inside).cumulative_return
    return result


def _receipt_bound_periods(
    account: Mapping[date, Decimal],
    reference: Mapping[date, Decimal],
    window: Mapping[date, Decimal],
    *,
    receipt: Mapping[str, Any] | None,
) -> tuple[dict[date, Decimal], str | None]:
    if receipt is None:
        return _compound_to_reference_periods(account, reference, window), None
    dates = sorted(reference)
    opens = dict(zip(dates[1:], dates))
    spans = [
        ReturnSpan(
            date.fromisoformat(str(item["span_start_date"])),
            date.fromisoformat(str(item["span_end_date"])),
            _decimal(item["return"], "epoch HPR span return"),
        )
        for item in receipt["span_return_receipts"]
    ]
    account_dates = sorted(account)
    result: dict[date, Decimal] = {}
    for day in sorted(window):
        if day not in opens:
            return {}, "RECEIPT_BOUND_REFERENCE_OPENING_UNPROVEN"
        opening = opens[day]
        period_spans = [
            span for span in spans if span.start_date < day and span.end_date > opening
        ]
        if any(
            span.start_date < opening or span.end_date > day for span in period_spans
        ):
            return {}, "RECEIPT_BOUND_COARSE_PERIOD_CUTS_HPR_SPAN"
        daily = [
            (item, account[item]) for item in account_dates if opening < item <= day
        ]
        if not daily:
            continue
        try:
            # Validate/compound real observations only. A span may start at the
            # reference opening; retain that actual anchor in the kernel and
            # cancel all daily factors outside (opening, day] afterwards.
            with localcontext() as context:
                context.prec = 34
                full = splice_spans(
                    sorted(account.items()), period_spans
                ).cumulative_return
                outside = _cumulative(
                    [
                        value
                        for observed, value in sorted(account.items())
                        if not opening < observed <= day
                    ]
                )
                result[day] = (Decimal(1) + full) / (Decimal(1) + outside) - Decimal(1)
        except ReturnsError as exc:
            raise AttributionValidationError(
                "receipt-bound coarse period is invalid"
            ) from exc
    return result, None


def _cumulative(values: Sequence[Decimal]) -> Decimal:
    wealth = Decimal(1)
    with localcontext() as context:
        context.prec = 34
        for value in values:
            wealth *= Decimal(1) + value
    return wealth - Decimal(1)


def _comparison_metrics(
    account: Mapping[date, Decimal],
    reference: Mapping[date, Decimal],
    periods_per_year: int,
    *,
    complete_cumulative: Decimal | None = None,
) -> dict[str, Any]:
    common = sorted(set(account) & set(reference))
    if len(common) < 2:
        raise AttributionValidationError("comparison lacks two exact common dates")
    reference_dec = [reference[day] for day in common]
    account_values = [float(account[day]) for day in common]
    reference_values = [float(value) for value in reference_dec]
    active = [left - right for left, right in zip(account_values, reference_values)]
    tracking_error = statistics.stdev(active) * math.sqrt(periods_per_year)
    active_return = statistics.fmean(active) * periods_per_year
    information_ratio = active_return / tracking_error if tracking_error > 0 else None
    reference_variance = statistics.pvariance(reference_values)
    beta = (
        sum(
            (left - statistics.fmean(account_values))
            * (right - statistics.fmean(reference_values))
            for left, right in zip(account_values, reference_values)
        )
        / len(common)
        / reference_variance
        if reference_variance > 0
        else None
    )
    correlation = (
        statistics.correlation(account_values, reference_values)
        if statistics.pstdev(account_values) > 1e-15
        and statistics.pstdev(reference_values) > 1e-15
        else None
    )
    upside = [index for index, value in enumerate(reference_values) if value > 0]
    downside = [index for index, value in enumerate(reference_values) if value < 0]
    upside_capture = (
        statistics.fmean(account_values[index] for index in upside)
        / statistics.fmean(reference_values[index] for index in upside)
        if upside and statistics.fmean(reference_values[index] for index in upside) != 0
        else None
    )
    downside_capture = (
        statistics.fmean(account_values[index] for index in downside)
        / statistics.fmean(reference_values[index] for index in downside)
        if downside
        and statistics.fmean(reference_values[index] for index in downside) != 0
        else None
    )
    relative_wealth = 1.0
    peak = 1.0
    relative_drawdown = 0.0
    for account_return, reference_return in zip(account_values, reference_values):
        relative_wealth *= (1 + account_return) / (1 + reference_return)
        peak = max(peak, relative_wealth)
        relative_drawdown = min(relative_drawdown, relative_wealth / peak - 1)
    try:
        account_cumulative = splice_spans(sorted(account.items())).cumulative_return
    except ReturnsError as exc:
        raise AttributionValidationError(
            f"account live series is invalid: {exc}"
        ) from exc
    if complete_cumulative is not None:
        account_cumulative = complete_cumulative
    reference_cumulative = _cumulative(reference_dec)
    return {
        "account_cumulative_return": _decimal_text(account_cumulative),
        "active_return_annualized": _decimal_text(active_return),
        "beta": _decimal_text(beta),
        "correlation": _decimal_text(correlation),
        "cumulative_gap": _decimal_text(account_cumulative - reference_cumulative),
        "downside_capture": _decimal_text(downside_capture),
        "information_ratio": _decimal_text(information_ratio),
        "observations": len(common),
        "observed_end": common[-1].isoformat(),
        "observed_start": common[0].isoformat(),
        "reference_cumulative_return": _decimal_text(reference_cumulative),
        "relative_drawdown": _decimal_text(relative_drawdown),
        "tracking_error_annualized": _decimal_text(tracking_error),
        "upside_capture": _decimal_text(upside_capture),
    }


def _accepted_comparison(
    comparison: Mapping[str, Any],
    strategy_id: str,
    account_live_series: Mapping[str, Mapping[date, Decimal]],
    reference_series: Mapping[str, Mapping[date, Decimal]],
    reference_only_history: Mapping[str, str],
    series_identities: Mapping[str, tuple[str, str, str]],
    epoch_receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    accepted = dict(comparison)
    status = accepted.get("status")
    metrics = None
    legacy_delta = None
    legacy_disposition = "NOT_APPLICABLE"
    account_source = None
    receipt_reason = None
    if status in AVAILABLE_STATUSES:
        # The account values come from the baseline's own ACCOUNT_LIVE series
        # keyed by strategy_id (B3: never a separately retained return-source
        # row while a live series exists); account_series_id is provenance.
        # Only a strategy with no live row at all, which the baseline admits
        # as history only, reads its retained series by account_series_id,
        # and the row discloses that in account_series_source.
        account_id = _text(
            accepted.get("account_series_id"), "comparison.account_series_id"
        )
        reference_id = _text(
            accepted.get("reference_series_id"), "comparison.reference_series_id"
        )
        if reference_id not in reference_series:
            raise AttributionValidationError("comparison series is missing")
        account_full_series = account_live_series.get(strategy_id)
        account_source = ACCOUNT_SOURCE_BASELINE_LIVE
        if not account_full_series:
            account_full_series = _retained_account_history(
                strategy_id,
                account_id,
                reference_only_history,
                reference_series,
                series_identities,
            )
            account_source = ACCOUNT_SOURCE_RETAINED_HISTORY
        start = (
            None
            if accepted.get("observed_start") is None
            else date.fromisoformat(
                _text(accepted.get("observed_start"), "comparison.observed_start")
            )
        )
        end = (
            None
            if accepted.get("observed_end") is None
            else date.fromisoformat(
                _text(accepted.get("observed_end"), "comparison.observed_end")
            )
        )
        if (start is None) != (end is None) or (
            start is not None and end is not None and end < start
        ):
            raise AttributionValidationError("comparison interval is invalid")
        reference_window = {
            day: value
            for day, value in reference_series[reference_id].items()
            if (start is None or day >= start) and (end is None or day <= end)
        }
        periods_per_year = int(accepted.get("periods_per_year", 0))
        if periods_per_year < DAILY_PERIODS_PER_YEAR:
            # A reference coarser than daily reports one return per period, so the
            # account's daily returns are compounded over the same periods before
            # the exact-date pairing (T6-F2); daily comparisons are unchanged.
            account_window, receipt_reason = _receipt_bound_periods(
                account_full_series,
                reference_series[reference_id],
                reference_window,
                receipt=epoch_receipts.get(strategy_id),
            )
            if receipt_reason is not None:
                accepted["status"] = NOT_READY_STATUS
                accepted["not_ready_reason"] = receipt_reason
                return {
                    **accepted,
                    "account_series_source": account_source,
                    "legacy_baseline_delta": legacy_delta,
                    "legacy_baseline_disposition": legacy_disposition,
                    "metrics": None,
                }
        else:
            account_window = {
                day: value
                for day, value in account_full_series.items()
                if (start is None or day >= start) and (end is None or day <= end)
            }
        complete_cumulative = None
        receipt = epoch_receipts.get(strategy_id)
        if receipt is not None and periods_per_year >= DAILY_PERIODS_PER_YEAR:
            if not account_window:
                raise AttributionValidationError(
                    "receipt-bound comparison has no daily observations"
                )
            first = start if start is not None else min(account_window)
            last = end if end is not None else max(account_window)
            spans = [
                ReturnSpan(
                    date.fromisoformat(item["span_start_date"]),
                    date.fromisoformat(item["span_end_date"]),
                    _decimal(item["return"], "epoch HPR span return"),
                )
                for item in receipt["span_return_receipts"]
                if date.fromisoformat(item["span_start_date"]) < last
                and date.fromisoformat(item["span_end_date"]) > first
            ]
            if any(span.start_date < first or span.end_date > last for span in spans):
                accepted["status"] = NOT_READY_STATUS
                accepted["not_ready_reason"] = "RECEIPT_BOUND_COMPARISON_CUTS_HPR_SPAN"
                return {
                    **accepted,
                    "account_series_source": account_source,
                    "legacy_baseline_delta": None,
                    "legacy_baseline_disposition": legacy_disposition,
                    "metrics": None,
                }
            try:
                # The admitted complete path supplies real anchors on both sides
                # of each span. Cancel only actual daily factors outside this
                # inclusive comparison window; do not invent an endpoint row.
                with localcontext() as context:
                    context.prec = 34
                    full = splice_spans(
                        sorted(account_full_series.items()), spans
                    ).cumulative_return
                    outside = _cumulative(
                        [
                            value
                            for observed, value in sorted(account_full_series.items())
                            if not first <= observed <= last
                        ]
                    )
                    complete_cumulative = (Decimal(1) + full) / (
                        Decimal(1) + outside
                    ) - Decimal(1)
            except ReturnsError as exc:
                raise AttributionValidationError(
                    "receipt-bound comparison interval is invalid"
                ) from exc
        metrics = _comparison_metrics(
            account_window,
            reference_window,
            periods_per_year,
            complete_cumulative=complete_cumulative,
        )
        expected_gap = accepted.get("expected_legacy_cumulative_gap")
        if expected_gap is not None:
            legacy_delta_value = _decimal(
                metrics["cumulative_gap"], "computed cumulative gap"
            ) - _decimal(expected_gap, "expected legacy cumulative gap")
            legacy_delta = _decimal_text(legacy_delta_value)
            legacy_disposition = (
                "INDEPENDENTLY_REPRODUCED"
                if abs(legacy_delta_value) <= Decimal("0.000000000001")
                else "CORRECTED_BY_CURRENT_EXACT_DATE_SOURCE"
            )
    elif status == "UNAVAILABLE":
        if (
            accepted.get("account_series_id") is not None
            or accepted.get("reference_series_id") is not None
        ):
            raise AttributionValidationError(
                "unavailable comparison cannot name comparison series"
            )
    else:
        raise AttributionValidationError("comparison status differs")
    return {
        **accepted,
        "account_series_source": account_source,
        "legacy_baseline_delta": legacy_delta,
        "legacy_baseline_disposition": legacy_disposition,
        "metrics": metrics,
    }


def build_catalog(
    baseline_root: Path,
    spec_path: Path,
    *,
    baseline_generation_id: str | None = None,
    dependence_source_root: Path | None = None,
    dependence_selected_inputs: Path | None = None,
) -> tuple[dict[str, Any], bytes, list[SourceArtifact]]:
    spec = _load_json(spec_path, "attribution spec")
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise AttributionValidationError("attribution spec schema is unsupported")
    as_of = date.fromisoformat(_text(spec.get("as_of_date"), "spec.as_of_date"))
    baseline, baseline_identity, baseline_path, baseline_generation_artifact = (
        _baseline(baseline_root, baseline_generation_id)
    )
    if baseline.get("as_of_date") != as_of.isoformat():
        raise AttributionValidationError("spec as_of_date differs from baseline")
    strategies = {
        _text(item.get("strategy_id"), "strategy.strategy_id"): item
        for item in baseline.get("strategies", [])
        if isinstance(item, dict)
    }
    if len(strategies) != STRATEGY_COUNT:
        raise AttributionValidationError(
            "baseline must contain exactly 9 retained strategy registry records"
        )
    source = spec.get("return_source")
    if not isinstance(source, dict):
        raise AttributionValidationError("return_source must be an object")
    source_path = Path(_text(source.get("path"), "return_source.path"))
    source_hash, source_size = _verify_hash(
        source_path, source.get("expected_sha256"), "return source"
    )
    manifest_path = Path(
        _text(source.get("source_manifest_path"), "source_manifest_path")
    )
    manifest_hash, manifest_size = _verify_hash(
        manifest_path,
        source.get("source_manifest_expected_sha256"),
        "source manifest",
    )
    source_manifest = _load_json(manifest_path, "source manifest")
    if source_manifest.get("verification") != "VERIFIED":
        raise AttributionValidationError("source manifest is not verified")
    try:
        source_manifest_as_of = date.fromisoformat(
            _text(source_manifest.get("as_of_date"), "source_manifest.as_of_date")
        )
    except ValueError as exc:
        raise AttributionValidationError(
            "source manifest as-of date is invalid"
        ) from exc
    series, series_identities = _rows(source_path)
    account_live_series = _account_live_series(baseline)
    epoch_receipts, dependence_artifacts = _epoch_receipts(
        baseline, baseline_path, dependence_source_root, dependence_selected_inputs
    )
    reference_only_history = _reference_only_account_history(baseline)
    benchmark_rows = spec.get("benchmark_assignments")
    benchmark_comparison_rows = spec.get("benchmark_comparisons")
    comparison_rows = spec.get("comparisons")
    if (
        not isinstance(benchmark_rows, list)
        or not isinstance(benchmark_comparison_rows, list)
        or not isinstance(comparison_rows, list)
    ):
        raise AttributionValidationError("strategy rows must be arrays")
    if any(
        len(rows) != STRATEGY_COUNT
        for rows in (benchmark_rows, benchmark_comparison_rows, comparison_rows)
    ):
        raise AttributionValidationError(
            "attribution requires exactly 9 retained strategy registry rows"
        )
    benchmark_by_id = {
        _text(item.get("strategy_id"), "benchmark.strategy_id"): item
        for item in benchmark_rows
        if isinstance(item, dict)
    }
    comparison_by_id = {
        _text(item.get("strategy_id"), "comparison.strategy_id"): item
        for item in comparison_rows
        if isinstance(item, dict)
    }
    benchmark_comparison_by_id = {
        _text(item.get("strategy_id"), "benchmark comparison.strategy_id"): item
        for item in benchmark_comparison_rows
        if isinstance(item, dict)
    }
    if any(
        set(rows) != set(strategies)
        for rows in (
            benchmark_by_id,
            benchmark_comparison_by_id,
            comparison_by_id,
        )
    ):
        raise AttributionValidationError("attribution strategy boundary differs")
    accepted_benchmarks: list[dict[str, Any]] = []
    accepted_benchmark_comparisons: list[dict[str, Any]] = []
    accepted_comparisons: list[dict[str, Any]] = []
    allocations = {
        str(item.get("strategy_id")): item
        for item in baseline.get("allocations", [])
        if isinstance(item, dict) and item.get("scope") == "SYSTEMATIC"
    }
    costs = {
        str(item.get("strategy_id")): item
        for item in baseline.get("costs", [])
        if isinstance(item, dict)
    }
    accepted_costs: list[dict[str, Any]] = []
    for strategy_id in sorted(strategies):
        benchmark = dict(benchmark_by_id[strategy_id])
        if benchmark.get("status") == "EVIDENCED" and (
            not benchmark.get("benchmark_id") or not benchmark.get("version_ref")
        ):
            raise AttributionValidationError(
                "evidenced benchmark requires benchmark and version"
            )
        if (
            benchmark.get("status") != "EVIDENCED"
            and benchmark.get("benchmark_id") is not None
        ):
            raise AttributionValidationError(
                "unavailable benchmark cannot name an accepted benchmark"
            )
        accepted_benchmarks.append(benchmark)
        benchmark_comparison = dict(benchmark_comparison_by_id[strategy_id])
        if benchmark.get("status") == "EVIDENCED":
            if benchmark_comparison.get("status") not in AVAILABLE_STATUSES:
                raise AttributionValidationError(
                    "evidenced benchmark requires an available comparison"
                )
            if benchmark_comparison.get("reference_series_id") != benchmark.get(
                "benchmark_id"
            ):
                raise AttributionValidationError(
                    "benchmark comparison reference differs from assignment"
                )
            if (
                benchmark_comparison.get("version_compatibility_status")
                != "PROVEN_COMPATIBLE"
            ):
                raise AttributionValidationError(
                    "benchmark return version must be proven compatible"
                )
        elif benchmark_comparison.get("status") != "UNAVAILABLE":
            raise AttributionValidationError(
                "unavailable benchmark assignment cannot produce metrics"
            )
        accepted_benchmark_comparisons.append(
            _accepted_comparison(
                benchmark_comparison,
                strategy_id,
                account_live_series,
                series,
                reference_only_history,
                series_identities,
                epoch_receipts,
            )
        )
        accepted_comparisons.append(
            _accepted_comparison(
                comparison_by_id[strategy_id],
                strategy_id,
                account_live_series,
                series,
                reference_only_history,
                series_identities,
                epoch_receipts,
            )
        )
        allocation = allocations[strategy_id]
        current_value = _decimal(
            allocation["current_value"]["value"], "allocation.current_value"
        )
        cost = costs.get(strategy_id, {})
        annual_cost = (
            None
            if cost.get("annual_external_cost_usd") is None
            else _decimal(cost["annual_external_cost_usd"], "annual cost")
        )
        accepted_costs.append(
            {
                "after_tax_annual_cost_rate": None,
                "after_tax_status": "UNAVAILABLE_TAX_LOT_RATE_AND_REALIZATION_EVIDENCE_INCOMPLETE",
                "annual_external_cost_rate_pre_tax": (
                    None
                    if annual_cost is None or current_value == 0
                    else _decimal_text(annual_cost / current_value)
                ),
                "annual_external_cost_usd": _decimal_text(annual_cost),
                "cost_status": cost.get("cost_status", "unknown"),
                "current_sleeve_value_usd": _decimal_text(current_value),
                "fees_status": "EVIDENCED_PRE_TAX"
                if annual_cost is not None
                else "UNAVAILABLE",
                "strategy_id": strategy_id,
            }
        )
    catalog = {
        "as_of_date": as_of.isoformat(),
        "baseline": baseline_identity,
        "benchmark_assignments": accepted_benchmarks,
        "benchmark_comparisons": accepted_benchmark_comparisons,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "comparisons": accepted_comparisons,
        "costs": accepted_costs,
        "source_manifest_sha256": manifest_hash,
        "source_returns_sha256": source_hash,
    }
    artifacts = [
        SourceArtifact.from_path(
            baseline_path,
            artifact_id="input/portfolio-baseline.json",
            as_of_date=as_of,
            schema_name="portfolio-baseline-catalog",
            schema_version="1",
        ),
        baseline_generation_artifact,
        SourceArtifact(
            artifact_id="input/attribution-returns.csv",
            sha256=source_hash,
            size_bytes=source_size,
            as_of_date=as_of,
            schema_name="strategy-attribution-return-source",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="input/source-manifest.json",
            sha256=manifest_hash,
            size_bytes=manifest_size,
            as_of_date=source_manifest_as_of,
            schema_name="strategy-attribution-source-manifest",
            schema_version="1",
        ),
    ]
    return catalog, canonical_json_bytes(catalog), artifacts + dependence_artifacts


def stage_catalog(
    baseline_root: Path,
    spec_path: Path,
    output_root: Path,
    *,
    baseline_generation_id: str | None = None,
    dependence_source_root: Path | None = None,
    dependence_selected_inputs: Path | None = None,
) -> dict[str, Any]:
    catalog, catalog_bytes, artifacts = build_catalog(
        baseline_root,
        spec_path,
        baseline_generation_id=baseline_generation_id,
        dependence_source_root=dependence_source_root,
        dependence_selected_inputs=dependence_selected_inputs,
    )
    as_of = date.fromisoformat(catalog["as_of_date"])
    catalog_artifact = SourceArtifact(
        artifact_id=f"normalized/{CATALOG_NAME}",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=as_of,
        schema_name="strategy-attribution",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=max(item.as_of_date for item in [*artifacts, catalog_artifact]),
        adapter=AdapterIdentity("portfolio-attribution-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "nine retained registry strategies (seven REQUIRED active current, Echo "
                "DEPRECATED_HISTORY_ONLY, Composer IMMATERIAL_EXCLUDED) with exact-date "
                "metrics or explicit unavailable dispositions"
            ),
            fact_types=(
                "benchmark assignment",
                "benchmark-relative attribution",
                "cost",
                "provider-account attribution",
            ),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=tuple([*artifacts, catalog_artifact]),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / CATALOG_NAME
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise AttributionValidationError("staged catalog differs")
    if not catalog_path.exists():
        catalog_path.write_bytes(catalog_bytes)
    return {
        "available_comparisons": sum(
            row["metrics"] is not None for row in catalog["comparisons"]
        ),
        "available_benchmark_comparisons": sum(
            row["metrics"] is not None for row in catalog["benchmark_comparisons"]
        ),
        "catalog_sha256": catalog_artifact.sha256,
        "generation_id": manifest.generation_id,
        "strategy_count": len(catalog["comparisons"]),
    }


def publish_catalog(
    baseline_root: Path,
    spec_path: Path,
    output_root: Path,
    generation_id: str,
    *,
    baseline_generation_id: str | None = None,
    dependence_source_root: Path | None = None,
    dependence_selected_inputs: Path | None = None,
) -> dict[str, Any]:
    staged = stage_catalog(
        baseline_root,
        spec_path,
        output_root,
        baseline_generation_id=baseline_generation_id,
        dependence_source_root=dependence_source_root,
        dependence_selected_inputs=dependence_selected_inputs,
    )
    if staged["generation_id"] != generation_id:
        raise AttributionValidationError("rebuilt generation differs")
    pointer = publish_generation(output_root, generation_id)
    return {**staged, "publication_state": pointer.state.value}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--baseline-root", type=Path, required=True)
        command.add_argument("--baseline-generation-id")
        command.add_argument("--dependence-source-root", type=Path)
        command.add_argument("--dependence-selected-inputs", type=Path)
        command.add_argument("--spec", type=Path, required=True)
        command.add_argument("--output-root", type=Path, required=True)
        if name == "publish":
            command.add_argument("--generation-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            result = stage_catalog(
                args.baseline_root,
                args.spec,
                args.output_root,
                baseline_generation_id=args.baseline_generation_id,
                dependence_source_root=args.dependence_source_root,
                dependence_selected_inputs=args.dependence_selected_inputs,
            )
        else:
            result = publish_catalog(
                args.baseline_root,
                args.spec,
                args.output_root,
                args.generation_id,
                baseline_generation_id=args.baseline_generation_id,
                dependence_source_root=args.dependence_source_root,
                dependence_selected_inputs=args.dependence_selected_inputs,
            )
    except (AttributionValidationError, OSError, ValueError) as exc:
        print(f"portfolio-attribution: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
