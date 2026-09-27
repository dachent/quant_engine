"""Native retained-input preparation for the supported attended runner.

This module deliberately composes existing source readers and producers.  It is
not a second pipeline: its only output is the schema-1 source-run handoff that
``portfolio_pipeline_materialize attended`` already consumes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict, cast

from quant_engine.core import run_storage
from quant_engine.core.contracts import canonical_json_bytes


class AttendedPreparationError(ValueError):
    """Pinned native input QC or preparation handoff is not admissible."""


ROOT = Path(__file__).resolve().parents[1]


class _HistoryAuthorities(TypedDict):
    financial_input_contract_path: Path
    portfolio123_version_evidence_path: Path
    provider_admission_receipt_path: Path
    provider_ledger_catalog_path: Path
    provider_ledger_manifest_path: Path


# This is the executable preparation graph.  The controller graph derives its
# preparation view from this declaration; do not maintain a second diagram.
PREPARATION_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "selected-history-normalization": (),
    "dependence": ("selected-history-normalization",),
    "plan-live-only": ("selected-history-normalization",),
    "plan-live-plus-backtest": ("selected-history-normalization",),
    "ledger": ("dependence", "plan-live-only", "plan-live-plus-backtest"),
    "baseline": ("ledger",),
    "epoch": ("ledger", "baseline"),
    "hwm": ("epoch", "ledger", "baseline"),
    "capital": ("epoch", "ledger"),
    "attribution": ("ledger", "plan-live-only"),
    "final-handoff": (
        "selected-history-normalization",
        "dependence",
        "plan-live-only",
        "plan-live-plus-backtest",
        "ledger",
        "baseline",
        "epoch",
        "hwm",
        "capital",
        "attribution",
    ),
}

# Every runtime alias used by preparation checkpoint identity resolves through
# this one repository-relative registry.  The layered-history dependency alias
# deliberately points at its owning declaration below rather than copying its
# list here.
PREPARATION_RUNTIME_PATHS: dict[str, str] = {
    "account_manifest_generation": "tools/build_account_manifest_generation.py",
    "account_manifest_schema": "schemas/portfolio/account-strategy-manifest.schema.json",
    "account_strategy_manifest": "tools/account_strategy_manifest.py",
    "attended_preparation": "tools/portfolio_attended_preparation.py",
    "baseline_producer": "tools/portfolio_baseline.py",
    "capital_input": "ops/mvp2-release/scripts/build_capital_input.py",
    "core_authority": "packages/core/src/quant_engine/core/authority.py",
    "core_publication": "packages/core/src/quant_engine/core/publication.py",
    "current_dependence": "tools/portfolio_current_dependence.py",
    "current_plan": "tools/portfolio_current_decomposition_plan.py",
    "dependence": "contracts/portfolio-dependence-policy-v1.json",
    "dependence_schema": "schemas/portfolio/dependence-policy.schema.json",
    "financial_outcomes": "tools/portfolio_financial_outcomes.py",
    "flow_classification": "contracts/account-strategy-flow-classification-policy-v1.json",
    "freshness": "tools/portfolio_freshness.py",
    "hwm_current": "contracts/portfolio-hwm-policy-v4.json",
    "hwm_calendar_authority": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_calendar_authority.py",
    "hwm_execution": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_governed_execution.py",
    "hwm_governed": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_governed.py",
    "hwm_input_kernel": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_input_builder.py",
    "hwm_inputs": "tools/portfolio_hwm_selected_inputs.py",
    "hwm_kernel": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm.py",
    "hwm_legacy": "contracts/portfolio-hwm-policy-v3.json",
    "invested_capital": "tools/portfolio_invested_capital.py",
    "layered_history_repository_dependencies": "tools/portfolio_target_series.py",
    "ledger_admission": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/provider_admission.py",
    "ledger_hwm_inputs": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_inputs.py",
    "ledger_models": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/models.py",
    "ledger_returns": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/returns.py",
    "performance": "contracts/portfolio-performance-policy-v1.json",
    "portfolio_returns": "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/returns.py",
    "provider_ledger": "tools/provider_ledger.py",
    "rebind_current_epoch_crosswalk": "tools/rebind_current_epoch_crosswalk.py",
    "release_builder": "tools/portfolio_release_builder.py",
    "stage_current_epoch_manifest": "tools/stage_current_epoch_manifest.py",
    "static_snapshot_coverage_receipts": "tools/static_snapshot_coverage_receipts.py",
    "target_series": "tools/portfolio_target_series.py",
    "verify_account_manifest_generation": "tools/verify_account_manifest_generation.py",
}

_PREPARATION_STAGE_CODE = {
    "selected-history-normalization": (
        "attended_preparation",
        "target_series",
        "layered_history_repository_dependencies",
    ),
    "dependence": ("attended_preparation", "current_dependence"),
    "plan-live-only": ("attended_preparation", "current_plan"),
    "plan-live-plus-backtest": ("attended_preparation", "current_plan"),
    "ledger": (
        "attended_preparation",
        "release_builder",
        "freshness",
        "core_authority",
        "core_publication",
    ),
    "baseline": (
        "attended_preparation",
        "release_builder",
        "baseline_producer",
        "ledger_models",
        "ledger_admission",
        "ledger_hwm_inputs",
    ),
    "epoch": (
        "attended_preparation",
        "rebind_current_epoch_crosswalk",
        "static_snapshot_coverage_receipts",
        "stage_current_epoch_manifest",
        "account_manifest_generation",
        "account_strategy_manifest",
        "portfolio_returns",
        "verify_account_manifest_generation",
        "release_builder",
    ),
    "hwm": (
        "attended_preparation",
        "hwm_inputs",
        "invested_capital",
        "hwm_execution",
        "hwm_calendar_authority",
        "hwm_input_kernel",
        "hwm_governed",
        "hwm_kernel",
    ),
    "capital": (
        "attended_preparation",
        "capital_input",
        "provider_ledger",
        "ledger_returns",
        "financial_outcomes",
        "hwm_inputs",
        "invested_capital",
    ),
    "attribution": ("attended_preparation", "current_plan", "release_builder"),
    "final-handoff": ("attended_preparation",),
}

_PREPARATION_STAGE_POLICY = {
    "selected-history-normalization": (),
    "dependence": ("dependence", "dependence_schema"),
    "plan-live-only": (),
    "plan-live-plus-backtest": (),
    "ledger": (),
    "baseline": ("hwm_legacy",),
    "epoch": ("flow_classification", "account_manifest_schema"),
    "hwm": ("hwm_current", "hwm_legacy", "performance"),
    "capital": (),
    "attribution": (),
    "final-handoff": (),
}

_PREPARATION_STAGE_OUTPUTS = {
    "selected-history-normalization": ("history_source",),
    "dependence": ("dependence_root", "selected_path"),
    "plan-live-only": ("plan_root",),
    "plan-live-plus-backtest": ("plan_root",),
    "ledger": ("ledger_generation", "recipe_path", "stage_result"),
    "baseline": ("baseline_generation", "stage_result"),
    "epoch": ("crosswalk", "epoch_generation", "receipt_root", "recipe_path"),
    "hwm": ("hwm_calendar", "hwm_input", "hwm_result"),
    "capital": ("capital_input", "capital_result"),
    "attribution": ("attribution", "stage_result"),
    "final-handoff": ("handoff", "source_run"),
}


def _capital_input_builder() -> Any:
    """Load the M6 parameterized API without importing an old task wrapper."""
    path = ROOT / "ops" / "mvp2-release" / "scripts" / "build_capital_input.py"
    spec = importlib.util.spec_from_file_location("attended_capital_input", path)
    if spec is None or spec.loader is None:
        raise AttendedPreparationError("capital input API is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_input_generation


def _read(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AttendedPreparationError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttendedPreparationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AttendedPreparationError(f"{label} must be an object")
    return value


def _pin(value: object, label: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "size_bytes"}:
        raise AttendedPreparationError(f"{label} pin differs")
    path = Path(str(value["path"])).resolve()
    if path.is_symlink() or not path.is_file():
        raise AttendedPreparationError(f"{label} is unavailable")
    content = path.read_bytes()
    if (
        hashlib.sha256(content).hexdigest() != value["sha256"]
        or len(content) != value["size_bytes"]
    ):
        raise AttendedPreparationError(f"{label} pin differs")
    return path


def _pins(value: object, label: str) -> None:
    """Verify every explicit file pin without turning locator roots into pins."""
    if isinstance(value, Mapping):
        if set(value) == {"path", "sha256", "size_bytes"}:
            _pin(value, label)
            return
        for key, nested in value.items():
            _pins(nested, f"{label}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _pins(nested, f"{label}[{index}]")


def _date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AttendedPreparationError(f"{label} is invalid")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise AttendedPreparationError(f"{label} is invalid") from exc


def _definition(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _directory_definition(path: Path) -> dict[str, object]:
    files = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise AttendedPreparationError("prepared directory contains a link")
        if child.is_file():
            content = child.read_bytes()
            files.append(
                {
                    "path": child.relative_to(path).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
    # The trailing newline matches the other four implementations of this
    # identity. `portfolio_pipeline_materialize._directory_identity` validates
    # the `history_generation` definition written here, so a difference in
    # framing alone makes the attended request unverifiable even when every row
    # agrees. `test_directory_identity_agrees_across_producers` pins them.
    encoded = (
        json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return {
        "path": str(path.resolve()),
        "file_count": len(files),
        "inventory_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json_bytes(dict(value))
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise AttendedPreparationError(f"prepared output differs: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def _original_observation_locators(
    observation: Mapping[str, Any], provider_rows: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """Recover provider roots only from the hash-pinned original observation."""

    pin_keys = {"path", "sha256", "size_bytes"}
    observed_keys = set(observation)
    if not observed_keys & pin_keys:
        # Lightweight unit rows need no filesystem locator. Actual admitted
        # receipts always carry all three binding fields.
        return {}
    required = {
        "captured_at_utc",
        "financial_cutoff",
        "path",
        "policy_contract_sha256",
        "providers",
        "sha256",
        "size_bytes",
    }
    if observed_keys != required:
        raise AttendedPreparationError("native source observation binding differs")
    original_path = _pin(
        {key: observation[key] for key in pin_keys},
        "original native source observation",
    )
    original = _read(original_path, "original native source observation")
    if set(original) != {
        "schema_version",
        "policy_contract_sha256",
        "financial_cutoff",
        "captured_at_utc",
        "providers",
    } or any(
        original.get(key) != observation.get(key)
        for key in ("policy_contract_sha256", "financial_cutoff", "captured_at_utc")
    ):
        raise AttendedPreparationError("original native source observation differs")
    selected = original.get("providers")
    if not isinstance(selected, Mapping) or set(selected) != set(provider_rows):
        raise AttendedPreparationError("original native provider set differs")
    locators: dict[str, Mapping[str, Any]] = {}
    for source_id, row in provider_rows.items():
        selected_row = selected.get(source_id)
        if not isinstance(selected_row, Mapping):
            raise AttendedPreparationError("original native provider row differs")
        if any(
            selected_row.get(key) != row.get(key)
            for key in (
                "catalog_sha256",
                "generation_id",
                "manifest_sha256",
                "qualification",
                "finalized_through",
            )
        ):
            raise AttendedPreparationError("original native provider identity differs")
        if not isinstance(selected_row.get("root"), str):
            raise AttendedPreparationError("original native provider root differs")
        locators[source_id] = selected_row
    return locators


def _native_snapshot(
    source_id: str, locator: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Read one source-selected catalog after checking its bound generation bytes."""

    try:
        import provider_ledger

        catalog_name = provider_ledger._PROVIDER_CATALOGS[source_id][0]  # noqa: SLF001
        root = Path(str(locator["root"])).resolve()
        generation = root / "generations" / str(locator["generation_id"])
        manifest = generation / "generation.json"
        catalog = generation / catalog_name
        if any(path.is_symlink() or not path.is_file() for path in (manifest, catalog)):
            raise AttendedPreparationError("original native generation is unavailable")
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != locator.get(
            "manifest_sha256"
        ):
            raise AttendedPreparationError("original native manifest differs")
        catalog_bytes = catalog.read_bytes()
        if hashlib.sha256(catalog_bytes).hexdigest() != locator.get("catalog_sha256"):
            raise AttendedPreparationError("original native catalog differs")
        payload = json.loads(catalog_bytes)
    except (
        ImportError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        raise AttendedPreparationError(
            "original native catalog is unavailable"
        ) from exc
    if not isinstance(payload, Mapping):
        raise AttendedPreparationError("original native catalog differs")
    snapshot = payload.get("position_snapshot_as_of")
    return snapshot if isinstance(snapshot, Mapping) else None


def _source_clock(source_phase: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    observation = source_phase.get("source_provenance")
    if not isinstance(observation, Mapping):
        raise AttendedPreparationError("native source observation is absent")
    providers = observation.get("providers")
    if isinstance(providers, Mapping):
        provider_rows = [(key, row) for key, row in providers.items()]
    elif isinstance(providers, list):
        provider_rows = [
            (row.get("provider_id"), row)
            for row in providers
            if isinstance(row, Mapping)
        ]
    else:
        provider_rows = []
    if not provider_rows:
        raise AttendedPreparationError("native source provider finality is absent")
    normalized_rows = {
        source_id: row
        for source_id, row in provider_rows
        if isinstance(source_id, str) and isinstance(row, Mapping)
    }
    if len(normalized_rows) != len(provider_rows):
        raise AttendedPreparationError("native source provider row differs")
    locators = _original_observation_locators(observation, normalized_rows)
    rows: list[dict[str, object]] = []
    limiting: list[str] = []
    applicable_final_dates: dict[str, str] = {}
    for source_id, row in sorted(provider_rows, key=lambda item: str(item[0])):
        if not isinstance(source_id, str) or not isinstance(row, Mapping):
            raise AttendedPreparationError("native source provider row differs")
        final = row.get("finalized_through", row.get("as_of_date"))
        final_date = _date(final, f"native source {source_id} finality")
        interpretation = row.get("finality_interpretation")
        if not isinstance(interpretation, Mapping) or not isinstance(
            interpretation.get("basis"), str
        ):
            raise AttendedPreparationError("native source finality differs")
        basis = interpretation["basis"]
        applicable = basis != "FINITE_SELECTED_MANIFEST_BOUNDARY"
        if applicable:
            applicable_final_dates[source_id] = final_date
        item: dict[str, object] = {
            "applies_to_latest_common_eod": applicable,
            "finality": {
                "basis": basis,
                "provider_finality_guarantee": interpretation.get(
                    "provider_finality_guarantee"
                ),
            },
            "provider_data_date": final_date,
            "source_id": source_id,
        }
        for source_key, target_key in (
            ("capture_start_utc", "native_capture_start_utc"),
            ("capture_end_utc", "native_capture_end_utc"),
            ("native_timezone", "timezone"),
            ("native_calendar", "native_calendar"),
        ):
            value = interpretation.get(source_key)
            if value is not None:
                item[target_key] = value
        for key in (
            "native_observed_at",
            "observed_at",
            "captured_at",
            "published_at",
            "timezone",
            "precision",
        ):
            if key in row:
                # Assembly captured_at_utc is intentionally excluded: it is not native data time.
                item["native_observed_at" if key == "observed_at" else key] = row[key]
        position = row.get("position_snapshot_as_of")
        if isinstance(position, Mapping):
            if "observed_at" in position:
                item["native_observed_at"] = position["observed_at"]
            if "provider_data_date" in position:
                item["provider_data_date"] = _date(
                    position["provider_data_date"], "position data date"
                )
            if "timezone" in position:
                item["timezone"] = position["timezone"]
        if (locator := locators.get(source_id)) is not None and (
            snapshot := _native_snapshot(source_id, locator)
        ) is not None:
            if "observed_at" in snapshot:
                item["native_observed_at"] = snapshot["observed_at"]
            if "provider_data_date" in snapshot:
                item["provider_data_date"] = _date(
                    snapshot["provider_data_date"], "position data date"
                )
            if "timezone" in snapshot:
                item["timezone"] = snapshot["timezone"]
        observed_at = item.get("native_observed_at")
        if observed_at is not None:
            if not isinstance(observed_at, str):
                raise AttendedPreparationError("native source observation differs")
            try:
                instant = datetime.fromisoformat(observed_at)
            except ValueError as exc:
                raise AttendedPreparationError(
                    "native source observation differs"
                ) from exc
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise AttendedPreparationError("native source observation differs")
            item["native_observed_at"] = instant.astimezone(UTC).isoformat()
            item.setdefault("precision", "TIMESTAMP")
            if source_id == "schwab":
                # Schwab position snapshots use the established New York
                # financial-day policy; this labels the native instant, not a
                # fabricated provider capture time.
                item.setdefault("timezone", "America/New_York")
        else:
            item.setdefault("precision", "DATE")
        rows.append(item)
    if not applicable_final_dates:
        raise AttendedPreparationError("native source current finality is absent")
    latest = min(applicable_final_dates.values())
    limiting = sorted(
        key for key, value in applicable_final_dates.items() if value == latest
    )
    if latest < cutoff:
        raise AttendedPreparationError(
            "native source finality does not support requested cutoff"
        )
    return {
        "latest_common_eod": latest,
        "selected_financial_cutoff": cutoff,
        "limiting_inputs": limiting,
        "source_data_as_of": rows,
    }


def _selected_history_authorities(files: Mapping[str, object]) -> _HistoryAuthorities:
    """Resolve the retained reader authorities from the selected source record."""

    names = (
        "provider_admission_receipt_path",
        "financial_input_contract_path",
        "provider_ledger_manifest_path",
        "provider_ledger_catalog_path",
        "portfolio123_version_evidence_path",
    )
    return cast(
        _HistoryAuthorities,
        {name: _pin(files.get(name), f"selected native {name}") for name in names},
    )


def _selected_dependence_handoff(
    *,
    config: Mapping[str, Any],
    selection: Mapping[str, object],
    history_source: Path,
    baseline_source: Path,
    contract: Path,
) -> dict[str, object]:
    """Rebind every selected-dependence authority from the admitted selection.

    The retained template contributes only its fixed schema; source-sensitive
    locators must come from the selection already admitted for this run.
    """

    files = selection.get("files")
    if not isinstance(files, Mapping):
        raise AttendedPreparationError("source selection pins are absent")
    authorities = _selected_history_authorities(files)
    selected = _read(
        _pin(config["dependence_selection_template"], "dependence selection"),
        "dependence selection",
    )
    required = {
        "schema_version",
        "history_generation_root",
        "allocation_generation_root",
        "provider_ledger_generation_root",
        "financial_input_contract_path",
        "provider_admission_receipt_path",
        "portfolio123_version_evidence_path",
    }
    if set(selected) != required or selected.get("schema_version") != 1:
        raise AttendedPreparationError("dependence selection schema differs")
    if authorities["financial_input_contract_path"] != contract.resolve():
        raise AttendedPreparationError("selected financial input contract differs")
    return {
        "schema_version": 1,
        "history_generation_root": str(history_source.resolve()),
        "allocation_generation_root": str(baseline_source.resolve()),
        "provider_ledger_generation_root": str(
            Path(str(config["provider_ledger_generation_root"])).resolve()
        ),
        "financial_input_contract_path": str(contract.resolve()),
        "provider_admission_receipt_path": str(
            authorities["provider_admission_receipt_path"].resolve()
        ),
        "portfolio123_version_evidence_path": str(
            authorities["portfolio123_version_evidence_path"].resolve()
        ),
    }


def _retained_history_requires_rebuild(
    history_root: Path,
    contract_path: Path,
    authorities: _HistoryAuthorities,
) -> bool:
    """Distinguish stale code- or ledger-bound derived history from invalid evidence.

    The verifier marks a TargetSeriesError as ``kind="staleness"`` only when a
    well-formed retained generation is one cycle behind by design: the code
    that defines it moved on (a recorded repository dependency differs), its
    current source admission proof binds a previous ledger generation, or its
    account-live returns no longer match the provider ledger after a
    semantics change. Each such generation is rebuilt from the frozen
    selected bytes. Every other TargetSeriesError is an integrity failure of
    the retained evidence itself (tampered artifact bytes, an invalid or
    non-canonical manifest, a differing inventory, an unreadable generation),
    and raw OSError/ValueError escapes are unreadable evidence; both fail
    closed without rebuilding.
    """

    import portfolio_target_series as history

    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        try:
            history.verify_layered_history_generation(
                history_root, contract_path, require_mode=mode, **authorities
            )
        except history.TargetSeriesError as exc:
            if exc.kind == "staleness":
                return True
            raise AttendedPreparationError(
                "retained layered history is invalid"
            ) from exc
        except (OSError, ValueError) as exc:
            raise AttendedPreparationError(
                "retained layered history is invalid"
            ) from exc
    return False


def _rebuild_layered_history(
    selection: Mapping[str, object],
    output_root: Path,
    release_commit: str,
    contract_path: Path,
    authorities: _HistoryAuthorities,
) -> Path:
    """Regenerate only stale code-bound history from the frozen selected bytes."""

    import portfolio_target_series as history

    raw_kwargs = selection.get("builder_kwargs")
    if not isinstance(raw_kwargs, Mapping):
        raise AttendedPreparationError("selected history builder inputs are absent")
    kwargs: dict[str, object] = {}
    for name, value in raw_kwargs.items():
        if not isinstance(name, str):
            raise AttendedPreparationError("selected history builder inputs differ")
        kwargs[name] = (
            Path(value) if name.endswith("_path") and value is not None else value
        )
    if kwargs.get("contract_path") != contract_path:
        raise AttendedPreparationError("selected history contract differs")
    if not isinstance(kwargs.get("as_of_date"), str):
        raise AttendedPreparationError("selected history cutoff differs")
    kwargs["repository_sha"] = release_commit

    build_history = cast(
        Callable[..., tuple[dict[str, bytes], bytes]],
        history.build_layered_history_generation,
    )
    artifacts, manifest = build_history(**kwargs)
    rebuilt: Path = history.write_layered_history_generation(
        output_root, artifacts, manifest
    )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        history.verify_layered_history_generation(
            rebuilt, contract_path, require_mode=mode, **authorities
        )
    return rebuilt


def _validate_hwm_authority_inputs(config: Mapping[str, Any]) -> None:
    """Admit existing HWM policy/calendar bindings before preparation has effects."""
    import portfolio_invested_capital as capital
    from quant_engine.portfolio_ledger.hwm_governed import (
        validate_governed_hwm_calendar_authority,
    )

    contract = _pin(config["financial_input_contract"], "financial input contract")
    calendar = _read(
        _pin(config["calendar_authority"], "calendar authority"), "calendar authority"
    )
    effective_policy = capital.governed_hwm_input_policy(
        (ROOT / "contracts" / "portfolio-hwm-policy-v4.json").read_bytes(),
        contract.read_bytes(),
        str(calendar["hwm_policy_sha256"]),
    )
    validate_governed_hwm_calendar_authority(
        calendar,
        hwm_policy_bytes=effective_policy,
        accepted_authority_sha256=str(calendar["authority_evidence_sha256"]),
    )


def _retained_public_factor_capture(
    definition: Mapping[str, Any], financial_data_cutoff: str
) -> dict[str, object]:
    """Freeze the already pinned public-factor acquisition identity for a plan."""

    manifest_path = _pin(definition["public_factor_manifest"], "public factor manifest")
    manifest = _read(manifest_path, "public factor manifest")
    acquisition_run = manifest.get("acquisition_run")
    if not isinstance(acquisition_run, Mapping):
        raise AttendedPreparationError("public factor acquisition run is absent")
    run_id = acquisition_run.get("run_id")
    if not isinstance(run_id, str):
        raise AttendedPreparationError("public factor acquisition run is absent")
    content = manifest_path.read_bytes()
    return {
        "schema_version": 1,
        "financial_data_cutoff": financial_data_cutoff,
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "manifest_size_bytes": len(content),
        "acquisition_run_id": run_id,
    }


def _validate_product_contract_currency(config: Mapping[str, Any]) -> None:
    """Refuse a pinned contract copy that the repository has since superseded.

    Retained inputs deliberately freeze the product contract so a generation stays
    replayable, but nothing re-derives those copies when the contract itself changes. The
    copy keeps hashing to its recorded value, so a pin check cannot see the drift; only
    `portfolio_current_dependence` compares the two, and it does so at the report stage,
    four stages downstream. This is the same comparison, made before anything executes.
    """
    pinned = _pin(config["financial_input_contract"], "financial input contract")
    product = ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
    try:
        pinned_bytes = pinned.read_bytes()
    except OSError as exc:
        # Name the absence. Leaking the raw OSError here reports a path rather
        # than the governance fact, and this runs before anything else reads it.
        raise AttendedPreparationError(
            f"selected financial input contract is unreadable at {pinned}"
        ) from exc
    product_bytes = product.read_bytes()
    if pinned_bytes == product_bytes:
        return
    pinned_digest = hashlib.sha256(pinned_bytes).hexdigest()
    product_digest = hashlib.sha256(product_bytes).hexdigest()
    raise AttendedPreparationError(
        "selected financial input contract differs from the product contract: "
        f"pinned {pinned_digest[:16]} at {pinned}, product {product_digest[:16]}. "
        "The retained copy is stale; re-admit the source chain against the current "
        "contract rather than relaxing this check."
    )


def _validated_inputs(
    preparation_spec_path: Path, requested_as_of: str, evaluated_on: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _read(preparation_spec_path.resolve(), "preparation specification")
    if config.get("schema_version") != 1:
        raise AttendedPreparationError("preparation specification schema differs")
    cutoff = _date(config.get("financial_data_cutoff"), "configured financial cutoff")
    if cutoff != _date(requested_as_of, "requested financial cutoff"):
        raise AttendedPreparationError(
            "requested financial cutoff differs from pinned input"
        )
    if _date(evaluated_on, "evaluated_on") < cutoff:
        raise AttendedPreparationError("evaluated_on precedes financial cutoff")
    _pins(config, "preparation specification")
    _validate_product_contract_currency(config)
    _validate_hwm_authority_inputs(config)
    # Capital consumes this replay specification transitively. Verify the exact
    # selected generation now, while no preparation output or calculation exists.
    import provider_ledger

    ledger_root = Path(str(config["provider_ledger_generation_root"])).resolve()
    replay = provider_ledger.verify_candidate(
        _pin(config["provider_ledger_spec"], "provider ledger replay specification"),
        ledger_root.name,
        output_root_override=ledger_root.parent.parent,
    )
    if replay.get("state") != "VERIFIED_STAGED":
        raise AttendedPreparationError("selected provider ledger replay differs")
    source_selection = _pin(config.get("source_selection"), "source selection")
    selection = _read(source_selection, "source selection")
    files = selection.get("files")
    if not isinstance(files, Mapping):
        raise AttendedPreparationError("source selection pins are absent")
    _pins(files, "selected native inputs")
    history_authorities = _selected_history_authorities(files)
    import portfolio_pipeline_materialize as materializer

    phase = materializer.validate_source_phase(
        requested_as_of=cutoff,
        provider_ledger_generation_root=Path(
            str(config["provider_ledger_generation_root"])
        ),
        financial_input_contract_path=_pin(
            config["financial_input_contract"], "financial input contract"
        ),
    )
    # Replay the retained history and every public-plan boundary before this
    # function returns.  The caller creates its output root only afterwards.
    import portfolio_current_decomposition_plan as plans

    contract = _pin(config["financial_input_contract"], "financial input contract")
    history_root = Path(
        str(config["retained_verified_generations"]["history"]["root"])
    ).resolve()
    rebuild_required = _retained_history_requires_rebuild(
        history_root, contract, history_authorities
    )
    if not rebuild_required:
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
            definition = config["plans"][mode]
            market = {
                key: _pin(definition[key], f"{mode} {key}")
                for key in (
                    "us_factors",
                    "europe_factors",
                    "style_returns",
                    "sector_returns",
                    "us_industries",
                )
            }
            plans.build_bundle(
                plan_path=_pin(definition["plan"], f"{mode} plan"),
                catalog_path=_pin(definition["catalog"], f"{mode} catalog"),
                financial_mvp_contract_path=contract,
                layered_history_generation_path=history_root,
                base_spec_path=_pin(
                    definition["base_spec_template"], f"{mode} base spec"
                ),
                base_source_root=Path(str(definition["base_source_template_root"])),
                market_paths=market,
                public_factor_manifest_path=_pin(
                    definition["public_factor_manifest"], f"{mode} public manifest"
                ),
                style_manifest_path=_pin(
                    definition["style_manifest"], f"{mode} style manifest"
                ),
                sector_manifest_path=_pin(
                    definition["sector_manifest"], f"{mode} sector manifest"
                ),
                evaluation_date=evaluated_on,
                evidence_mode=mode,
                assurance_profile="RELEASE_QC",
                retained_public_factor_capture=_retained_public_factor_capture(
                    definition, cutoff
                ),
                **history_authorities,
            )
    return config, {
        "source_phase": phase,
        "data_clock": _source_clock(phase, cutoff),
        "history_rebuild_required": rebuild_required,
    }


def plan_attended_inputs(
    *, preparation_spec_path: Path, requested_as_of: str, evaluated_on: str
) -> dict[str, Any]:
    """Run all source/input admission checks and declare clocks without writes."""
    _config, plan = _validated_inputs(
        preparation_spec_path, requested_as_of, evaluated_on
    )
    return {"status": "PLANNED", **plan}


def _accepted_intervals(
    generation: Mapping[str, Any], calendar: Mapping[str, Any], cutoff: str
) -> list[dict[str, str]]:
    memberships = generation.get("memberships")
    authority = calendar.get("authority_evidence_sha256")
    if not isinstance(memberships, list) or not isinstance(authority, str):
        raise AttendedPreparationError("governed HWM input or calendar differs")
    start = min(
        _date(row.get("effective_start"), "HWM membership start")
        for row in memberships
        if isinstance(row, Mapping)
    )
    day = date.fromisoformat(start)
    end = date.fromisoformat(cutoff)
    rows = []
    while day < end:
        next_day = day + timedelta(days=1)
        left, right = day.isoformat(), next_day.isoformat()
        rows.append(
            {
                "interval_id": f"{left}/{right}",
                "interval_start": left,
                "interval_end": right,
                "interval_evidence_sha256": hashlib.sha256(
                    f"{left}|{right}|{authority}".encode()
                ).hexdigest(),
            }
        )
        day = next_day
    return rows


def _maximum_age_days(contract: Mapping[str, Any]) -> int:
    source_segments = contract.get("source_segments")
    if not isinstance(source_segments, list):
        raise AttendedPreparationError("required HWM source policy is absent")
    found = []
    for source in source_segments:
        if not isinstance(source, Mapping):
            raise AttendedPreparationError("required HWM source policy differs")
        freshness = source.get("freshness")
        consumers = source.get("consumers")
        if (
            source.get("product_requirement") != "REQUIRED"
            or source.get("capture_status") != "CAPTURED_GOVERNED"
            or source.get("existence_status") != "EXISTS"
            or not isinstance(consumers, list)
            or "AS_HELD_COMPOSITE_HWM" not in consumers
            or not isinstance(freshness, Mapping)
        ):
            continue
        rule = freshness.get("rule")
        maximum = freshness.get("max_age_days")
        if (
            isinstance(rule, str)
            and rule.startswith("THROUGH_ACCEPTED_")
            and isinstance(maximum, int)
            and not isinstance(maximum, bool)
            and maximum >= 0
        ):
            found.append(maximum)
    if not found:
        raise AttendedPreparationError("required dated HWM source policy is absent")
    return min(found)


def _preparation_stage_names() -> set[str]:
    declarations = (
        PREPARATION_STAGE_DEPENDENCIES,
        _PREPARATION_STAGE_CODE,
        _PREPARATION_STAGE_POLICY,
        _PREPARATION_STAGE_OUTPUTS,
    )
    stage_names = set(PREPARATION_STAGE_DEPENDENCIES)
    if not stage_names or any(
        set(declaration) != stage_names for declaration in declarations
    ):
        raise AttendedPreparationError("preparation stage declaration differs")
    for stage, predecessors in PREPARATION_STAGE_DEPENDENCIES.items():
        if any(predecessor not in stage_names for predecessor in predecessors):
            raise AttendedPreparationError("preparation stage declaration differs")
    return stage_names


def _runtime_path(alias: str) -> Path:
    try:
        relative_path = PREPARATION_RUNTIME_PATHS[alias]
    except KeyError as exc:
        raise AttendedPreparationError("preparation runtime alias differs") from exc
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise AttendedPreparationError("preparation runtime alias differs")
    resolved = ROOT / path
    if resolved.is_symlink() or not resolved.is_file():
        raise AttendedPreparationError("preparation runtime path is unavailable")
    return resolved


def _layered_history_repository_dependency_identity() -> dict[str, object]:
    """Bind the target-series authority list without copying its owned paths."""

    import portfolio_target_series as target_series

    definitions: dict[str, object] = {}
    for relative_path in target_series.LAYERED_HISTORY_REPOSITORY_DEPENDENCIES:
        if not isinstance(relative_path, str):
            raise AttendedPreparationError("layered history runtime dependency differs")
        path = ROOT / relative_path
        if path.is_symlink() or not path.is_file():
            raise AttendedPreparationError(
                "layered history runtime dependency is unavailable"
            )
        definitions[relative_path] = _definition(path)
    if not definitions:
        raise AttendedPreparationError("layered history runtime dependency differs")
    return definitions


def _preparation_input_identity(
    *,
    preparation_spec_path: Path,
    config: Mapping[str, Any],
    plan: Mapping[str, Any],
    release_commit: str,
    evaluated_on: str,
) -> dict[str, Any]:
    """Bind every selected input, policy, and local producer code surface."""

    source_selection = _pin(config["source_selection"], "source selection")
    _preparation_stage_names()
    code_names = set().union(*_PREPARATION_STAGE_CODE.values())
    policy_names = set().union(*_PREPARATION_STAGE_POLICY.values())
    code = {
        name: _definition(_runtime_path(name))
        for name in sorted(code_names - {"layered_history_repository_dependencies"})
    }
    code["layered_history_repository_dependencies"] = (
        _layered_history_repository_dependency_identity()
    )
    return {
        "schema_version": 1,
        "code": code,
        "evaluation_date": evaluated_on,
        "financial_data_cutoff": config["financial_data_cutoff"],
        "input_config": _definition(preparation_spec_path.resolve()),
        "policy": {
            name: _definition(_runtime_path(name)) for name in sorted(policy_names)
        },
        "release_commit": release_commit,
        "source_phase": plan["source_phase"],
        "source_selection": _definition(source_selection),
    }


def _preparation_stage_inputs(
    config: Mapping[str, Any], release_commit: str
) -> dict[str, dict[str, Any]]:
    """Bind only each producer's direct selected inputs, not unrelated config bytes."""

    common = {"source_selection": config["source_selection"]}
    plans = config["plans"]
    return {
        "selected-history-normalization": {
            **common,
            "history": config["retained_verified_generations"]["history"],
        },
        "dependence": {
            **common,
            "baseline": config["retained_verified_generations"]["baseline"],
            "dependence_selection_template": config["dependence_selection_template"],
        },
        "plan-live-only": {**common, "plan": plans["LIVE_ONLY"]},
        "plan-live-plus-backtest": {
            **common,
            "plan": plans["LIVE_PLUS_BACKTEST"],
        },
        "ledger": {
            "base_factor_file": config["base_factor_file"],
            "baseline_spec_template": config["baseline_spec_template"],
            "implementation_reconciliation": config["implementation_reconciliation"],
            "implementation_return_attribution": config[
                "implementation_return_attribution"
            ],
            "legacy_panel_root": config["legacy_panel_root"],
            "provider_ledger_generation_root": config[
                "provider_ledger_generation_root"
            ],
            "release_commit": release_commit,
            "strategy_attribution": config["retained_verified_generations"][
                "attribution"
            ],
        },
        "baseline": {},
        "epoch": {
            "epoch_admission_policy": config["epoch_admission_policy"],
            "epoch_crosswalk_template": config["epoch_crosswalk_template"],
        },
        "hwm": {
            "calendar_authority": config["calendar_authority"],
            "epoch_admission_policy": config["epoch_admission_policy"],
            "financial_input_contract": config["financial_input_contract"],
        },
        "capital": {
            "capital_account_crosswalk": config["capital_account_crosswalk"],
            "financial_input_contract": config["financial_input_contract"],
            "provider_ledger_spec": config["provider_ledger_spec"],
        },
        "attribution": {},
        "final-handoff": {"release_commit": release_commit},
    }


def _reusable_preparation_handoff(
    *,
    root: Path,
    config: Mapping[str, Any],
    identity: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit only this runner's complete, unchanged private handoff."""

    stored_identity = _read(
        root / "preparation-input-identity.json", "preparation input identity"
    )
    if stored_identity not in (
        dict(identity),
        _preparation_run_identity(identity),
    ):
        raise AttendedPreparationError("preparation input identity differs")
    source_run = root / "preparation" / "source-run.json"
    handoff = _read(root / "preparation-handoff.json", "preparation handoff")
    definitions = handoff.get("financial_stage_inputs")
    required_flags = {
        "--hwm-governed-result",
        "--hwm-governed-input-generation",
        "--hwm-governed-calendar-authority",
        "--hwm-governed-admission-receipt",
        "--invested-capital-result",
        "--invested-capital-input-generation",
    }
    if not isinstance(definitions, Mapping) or set(definitions) != required_flags:
        raise AttendedPreparationError("prepared financial handoff differs")
    financial = {
        flag: _pin(definition, f"prepared {flag}")
        for flag, definition in definitions.items()
    }
    # Returned producer outputs, including the selected-source HWM calendar,
    # must remain under this run's private checkpointed root.
    for path in (source_run, *financial.values()):
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise AttendedPreparationError(
                "prepared handoff escapes its private root"
            ) from exc
    calendar_path = financial["--hwm-governed-calendar-authority"]
    if calendar_path != root / "preparation" / "hwm-calendar-authority.json":
        raise AttendedPreparationError("prepared selected calendar path differs")
    calendar = _read(calendar_path, "prepared selected calendar authority")
    hwm_checkpoint = _read(_checkpoint_path(root, "hwm"), "prepared HWM checkpoint")
    outputs = hwm_checkpoint.get("outputs")
    result = hwm_checkpoint.get("result")
    if (
        not isinstance(outputs, Mapping)
        or outputs.get("hwm_calendar") != _definition(calendar_path)
        or not isinstance(result, Mapping)
        or result.get("hwm_calendar") != str(calendar_path)
    ):
        raise AttendedPreparationError("prepared selected calendar checkpoint differs")
    hwm_input = _read(
        financial["--hwm-governed-input-generation"], "prepared HWM input"
    )
    if hwm_input.get("calendar_sha256") != calendar.get("authority_evidence_sha256"):
        raise AttendedPreparationError("prepared HWM input calendar binding differs")
    expected = {
        "schema_version": 1,
        "source_run": _definition(source_run),
        "financial_stage_inputs": {
            key: _definition(path) for key, path in sorted(financial.items())
        },
        "source_phase": plan["source_phase"],
        "data_clock": plan["data_clock"],
    }
    if handoff != expected:
        raise AttendedPreparationError("preparation handoff differs")
    source = _read(source_run, "prepared source run")
    attended = source.get("attended")
    if (
        source.get("schema_version") != 1
        or source.get("as_of_date") != identity["financial_data_cutoff"]
        or source.get("release_commit") != identity["release_commit"]
        or not isinstance(attended, Mapping)
        or attended.get("data_clock") != plan["data_clock"]
    ):
        raise AttendedPreparationError("prepared source run differs")
    return {
        "source_run": source_run,
        "financial_stage_inputs": financial,
        "data_clock": plan["data_clock"],
    }


def _verify_epoch_calculation_cutoff(
    *, crosswalk_path: Path, epoch_root: Path, cutoff: str
) -> None:
    """Require the selected cutoff in both the epoch input and produced manifest."""

    expected = _date(cutoff, "requested financial cutoff")
    crosswalk = _read(crosswalk_path, "prepared current-epoch crosswalk")
    manifest = _read(
        epoch_root / "outputs" / "account-strategy-manifest.json",
        "prepared account-strategy manifest",
    )
    if (
        _date(
            crosswalk.get("calculation_cutoff_date"),
            "prepared current-epoch crosswalk calculation cutoff",
        )
        != expected
        or _date(
            manifest.get("calculation_cutoff_date"),
            "prepared account-strategy manifest calculation cutoff",
        )
        != expected
    ):
        raise AttendedPreparationError("epoch calculation cutoff differs")


def _preparation_run_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Keep source-selection continuity separate from stage-local code changes."""

    # `evaluation_date` sits beside the cutoff because the two are different clocks and a
    # checkpoint computed under one evaluation date is not reusable under another: the plan
    # bundles take it as `evaluation_date`, the dependence spec as its `as_of_date`, and the
    # governed HWM result records it and derives freshness from it.
    keys = (
        "schema_version",
        "evaluation_date",
        "financial_data_cutoff",
        "source_phase",
        "source_selection",
    )
    if not all(key in identity for key in keys):
        return dict(identity)
    return {key: identity[key] for key in keys}


def _checkpoint_path(root: Path, stage: str) -> Path:
    return root / "preparation" / "checkpoints" / f"{stage}.json"


def _checkpoint_temporary_path(root: Path, stage: str) -> Path:
    path = _checkpoint_path(root, stage)
    return path.with_name(f".{path.name}.pending")


def _checkpoint_definition(path: Path) -> dict[str, object]:
    return _definition(path)


def _checkpoint_output(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise AttendedPreparationError("preparation checkpoint output is a link")
    if path.is_file():
        return _definition(path)
    if path.is_dir():
        return _directory_definition(path)
    raise AttendedPreparationError("preparation checkpoint output is unavailable")


def _verified_checkpoint_output(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise AttendedPreparationError(f"{label} differs")
    keys = set(value)
    if keys == {"path", "sha256", "size_bytes"}:
        _pin(value, label)
        return
    if keys == {"path", "file_count", "inventory_sha256"}:
        path = Path(str(value["path"])).resolve()
        if (
            path.is_symlink()
            or not path.is_dir()
            or _directory_definition(path) != value
        ):
            raise AttendedPreparationError(f"{label} differs")
        return
    raise AttendedPreparationError(f"{label} differs")


def _stage_identity(
    stage: str,
    run_identity: Mapping[str, Any],
    full_identity: Mapping[str, Any],
    completed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    _preparation_stage_names()
    try:
        predecessors = PREPARATION_STAGE_DEPENDENCIES[stage]
        code_names = _PREPARATION_STAGE_CODE[stage]
        policy_names = _PREPARATION_STAGE_POLICY[stage]
        output_names = _PREPARATION_STAGE_OUTPUTS[stage]
    except KeyError as exc:
        raise AttendedPreparationError("preparation stage declaration differs") from exc
    if any(predecessor not in completed for predecessor in predecessors):
        raise AttendedPreparationError(
            "preparation checkpoint dependency is incomplete"
        )
    return {
        "code": {name: full_identity["code"][name] for name in code_names},
        "inputs": dict(full_identity.get("stage_inputs", {}).get(stage, {})),
        "output_names": list(output_names),
        "policy": {name: full_identity["policy"][name] for name in policy_names},
        "predecessors": {
            predecessor: _checkpoint_definition(completed[predecessor]["path"])
            for predecessor in predecessors
        },
        "run_identity": dict(run_identity),
        "stage": stage,
    }


def _load_checkpoint(
    *,
    root: Path,
    stage: str,
    identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    path = _checkpoint_path(root, stage)
    if not path.exists():
        return None
    receipt = _read(path, f"preparation {stage} checkpoint")
    if (
        set(receipt)
        != {"input_identity", "outputs", "result", "schema_version", "stage"}
        or receipt.get("schema_version") != 1
        or receipt.get("stage") != stage
        or not isinstance(receipt.get("outputs"), Mapping)
        or not isinstance(receipt.get("result"), Mapping)
    ):
        raise AttendedPreparationError("preparation checkpoint identity differs")
    if set(receipt["outputs"]) != set(_PREPARATION_STAGE_OUTPUTS[stage]):
        raise AttendedPreparationError("preparation checkpoint output differs")
    if receipt.get("input_identity") != identity:
        return None
    for name, output in receipt["outputs"].items():
        if not isinstance(name, str):
            raise AttendedPreparationError("preparation checkpoint output differs")
        try:
            _verified_checkpoint_output(output, f"preparation {stage} {name}")
        except AttendedPreparationError:
            return None
    return {"path": path, "result": dict(receipt["result"])}


def _record_checkpoint(
    *,
    root: Path,
    stage: str,
    identity: Mapping[str, Any],
    result: Mapping[str, Any],
    outputs: Mapping[str, Path],
) -> dict[str, Any]:
    path = _checkpoint_path(root, stage)
    if path.exists():
        raise AttendedPreparationError("preparation checkpoint already exists")
    if set(outputs) != set(_PREPARATION_STAGE_OUTPUTS[stage]):
        raise AttendedPreparationError("preparation checkpoint output differs")
    receipt = {
        "input_identity": dict(identity),
        "outputs": {
            name: _checkpoint_output(output) for name, output in sorted(outputs.items())
        },
        "result": dict(result),
        "schema_version": 1,
        "stage": stage,
    }
    content = canonical_json_bytes(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _checkpoint_temporary_path(root, stage)
    if temporary.exists():
        raise AttendedPreparationError("preparation checkpoint temporary path exists")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except OSError as exc:
        raise AttendedPreparationError(
            "preparation checkpoint cannot be committed"
        ) from exc
    return {"path": path, "result": dict(result)}


def _preserve_uncheckpointed_stage(root: Path, stage: str) -> None:
    """Keep incomplete local producer bytes before a create-only retry."""

    paths = {
        "selected-history-normalization": (root / "preparation" / "history",),
        "dependence": (
            root / "preparation" / "dependence",
            root / "preparation" / "dependence-selected-inputs.json",
        ),
        "plan-live-only": (root / "preparation" / "plans" / "live_only",),
        "plan-live-plus-backtest": (
            root / "preparation" / "plans" / "live_plus_backtest",
        ),
        "ledger": (
            root / "preparation" / "staging-recipe.json",
            root / "stage-results" / "ledger.json",
            root / "stages" / "ledger",
        ),
        "baseline": (
            root / "stage-results" / "baseline.json",
            root / "stages" / "baseline",
        ),
        "epoch": (
            root / "preparation" / "current-epoch-crosswalk.json",
            root / "preparation" / "epoch-receipts",
            root / "preparation" / "release-recipe.json",
            root / "stages" / "epoch",
        ),
        "hwm": (
            root / "preparation" / "hwm-calendar-authority.json",
            root / "preparation" / "hwm-input-generation.json",
            root / "preparation" / "hwm-governed-result.json",
        ),
        "capital": (
            root / "preparation" / "capital-input-generation.json",
            root / "preparation" / "invested-capital-result.json",
        ),
        "attribution": (
            root / "stage-results" / "attribution.json",
            root / "stages" / "attribution",
        ),
        "final-handoff": (
            root / "preparation" / "source-run.json",
            root / "preparation-handoff.json",
        ),
    }[stage] + (_checkpoint_path(root, stage), _checkpoint_temporary_path(root, stage))
    existing = [path for path in paths if path.exists()]
    if not existing:
        return
    if any(path.is_symlink() for path in existing):
        raise AttendedPreparationError("incomplete preparation stage contains a link")
    archive_root = root / "preparation" / "partial" / stage
    index = 1
    destination = archive_root / str(index)
    while destination.exists():
        index += 1
        destination = archive_root / str(index)
    destination.mkdir(parents=True)
    for path in existing:
        os.replace(path, destination / path.name)


def _prepare_attended_inputs(
    *,
    preparation_spec_path: Path,
    output_root: Path,
    requested_as_of: str,
    evaluated_on: str,
    release_commit: str,
    resume: bool = False,
    observation_started: bool = False,
    validated: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one verified local preparation handoff using retained native inputs."""
    if len(release_commit) != 40:
        raise AttendedPreparationError("release commit differs")
    if validated is None:
        config, plan = _validated_inputs(
            preparation_spec_path, requested_as_of, evaluated_on
        )
    else:
        config, plan = validated
    root = output_root.resolve()
    identity = _preparation_input_identity(
        preparation_spec_path=preparation_spec_path,
        config=config,
        plan=plan,
        release_commit=release_commit,
        evaluated_on=evaluated_on,
    )
    run_identity = _preparation_run_identity(identity)
    if root.exists():
        if not resume and not observation_started:
            raise AttendedPreparationError(
                "preparation output already exists; use --resume"
            )
        if resume:
            stored_identity = _read(
                root / "preparation-input-identity.json", "preparation input identity"
            )
            if _preparation_run_identity(stored_identity) != run_identity:
                raise AttendedPreparationError("preparation input identity differs")
        else:
            _write(root / "preparation-input-identity.json", run_identity)
    else:
        root.mkdir(parents=True)
        _write(root / "preparation-input-identity.json", run_identity)
    identity["stage_inputs"] = _preparation_stage_inputs(config, release_commit)

    completed: dict[str, Mapping[str, Any]] = {}

    def reuse(stage: str) -> dict[str, Any] | None:
        stage_identity = _stage_identity(stage, run_identity, identity, completed)
        checkpoint = _load_checkpoint(root=root, stage=stage, identity=stage_identity)
        if checkpoint is None:
            if resume:
                _preserve_uncheckpointed_stage(root, stage)
            return None
        completed[stage] = checkpoint
        return cast(dict[str, Any], checkpoint["result"])

    def checkpoint(
        stage: str, result: Mapping[str, Any], outputs: Mapping[str, Path]
    ) -> dict[str, Any]:
        stage_identity = _stage_identity(stage, run_identity, identity, completed)
        recorded = _record_checkpoint(
            root=root,
            stage=stage,
            identity=stage_identity,
            result=result,
            outputs=outputs,
        )
        completed[stage] = recorded
        return dict(result)

    import portfolio_current_dependence as dependence
    import portfolio_current_decomposition_plan as plans
    import portfolio_financial_mvp_contract as financial_contract
    import portfolio_financial_outcomes as outcomes
    import portfolio_hwm_selected_inputs as hwm_inputs
    import portfolio_invested_capital as capital
    import portfolio_release_builder as release
    from quant_engine.portfolio_ledger.hwm import _json_safe as hwm_json_safe
    from quant_engine.portfolio_ledger.hwm_governed_execution import (
        compute_governed_as_held_composite_hwm,
        verify_governed_hwm_result,
    )
    from rebind_current_epoch_crosswalk import rebind_current_epoch_crosswalk
    from stage_current_epoch_manifest import stage_current_epoch_manifest
    from static_snapshot_coverage_receipts import issue_static_snapshot_receipts

    cutoff = _date(requested_as_of, "requested financial cutoff")
    source_clock = root / "preparation" / "source-clock.json"
    _write(
        source_clock,
        {
            "schema_version": 1,
            "source_phase": plan["source_phase"],
            "data_clock": plan["data_clock"],
        },
    )
    contract = _pin(config["financial_input_contract"], "financial input contract")
    selection = _read(
        _pin(config["source_selection"], "source selection"), "source selection"
    )
    files = selection.get("files")
    if not isinstance(files, Mapping):
        raise AttendedPreparationError("source selection pins are absent")
    history_authorities = _selected_history_authorities(files)
    ledger_source = Path(str(config["provider_ledger_generation_root"])).resolve()
    baseline_source = Path(
        str(config["retained_verified_generations"]["baseline"]["root"])
    ).resolve()
    attribution_source = Path(
        str(config["retained_verified_generations"]["attribution"]["root"])
    ).resolve()
    history_source = Path(
        str(config["retained_verified_generations"]["history"]["root"])
    ).resolve()
    history = reuse("selected-history-normalization")
    if history is not None:
        history_source = Path(str(history["history_source"])).resolve()
    else:
        if plan["history_rebuild_required"]:
            history_source = _rebuild_layered_history(
                selection,
                root / "preparation" / "history",
                release_commit,
                contract,
                history_authorities,
            )
        checkpoint(
            "selected-history-normalization",
            {"history_source": str(history_source)},
            {"history_source": history_source},
        )

    # The recipe is produced from retained accepted readers; all later stages use
    # the real private roots below, never a task-script output path.
    selected_path = root / "preparation" / "dependence-selected-inputs.json"
    dependence_checkpoint = reuse("dependence")
    if dependence_checkpoint is not None:
        selected_path = Path(str(dependence_checkpoint["selected_path"])).resolve()
        dependence_root = Path(str(dependence_checkpoint["dependence_root"])).resolve()
    else:
        selected = _selected_dependence_handoff(
            config=config,
            selection=selection,
            history_source=history_source,
            baseline_source=baseline_source,
            contract=contract,
        )
        _write(selected_path, selected)
        dependence_result = dependence.write_source(
            root / "preparation" / "dependence",
            contract_path=contract,
            policy_path=ROOT / "contracts" / "portfolio-dependence-policy-v1.json",
            history_generation_root=history_source,
            allocation_generation_root=baseline_source,
            evaluated_on=date.fromisoformat(evaluated_on),
            selected_inputs_path=selected_path,
        )
        dependence_root = Path(str(dependence_result["destination"]))
        dependence.verify_packaged_source(
            dependence_root,
            contract_path=contract,
            policy_path=ROOT / "contracts" / "portfolio-dependence-policy-v1.json",
            selected_inputs_path=selected_path,
        )
        checkpoint(
            "dependence",
            {
                "dependence_root": str(dependence_root),
                "selected_path": str(selected_path),
            },
            {"dependence_root": dependence_root, "selected_path": selected_path},
        )

    plan_roots: dict[str, Path] = {}
    for mode, definition in config["plans"].items():
        stage = "plan-live-only" if mode == "LIVE_ONLY" else "plan-live-plus-backtest"
        plan_checkpoint = reuse(stage)
        if plan_checkpoint is not None:
            plan_roots[mode] = Path(str(plan_checkpoint["plan_root"])).resolve()
            continue
        market = {
            key: _pin(definition[key], f"{mode} {key}")
            for key in (
                "us_factors",
                "europe_factors",
                "style_returns",
                "sector_returns",
                "us_industries",
            )
        }
        result = plans.write_bundle(
            root / "preparation" / "plans" / mode.lower(),
            plan_path=_pin(definition["plan"], f"{mode} plan"),
            catalog_path=_pin(definition["catalog"], f"{mode} catalog"),
            financial_mvp_contract_path=contract,
            layered_history_generation_path=history_source,
            base_spec_path=_pin(definition["base_spec_template"], f"{mode} base spec"),
            base_source_root=Path(str(definition["base_source_template_root"])),
            market_paths=market,
            public_factor_manifest_path=_pin(
                definition["public_factor_manifest"], f"{mode} public manifest"
            ),
            style_manifest_path=_pin(
                definition["style_manifest"], f"{mode} style manifest"
            ),
            sector_manifest_path=_pin(
                definition["sector_manifest"], f"{mode} sector manifest"
            ),
            evaluation_date=evaluated_on,
            evidence_mode=mode,
            assurance_profile="RELEASE_QC",
            retained_public_factor_capture=_retained_public_factor_capture(
                definition, cutoff
            ),
            **history_authorities,
        )
        plan_root = Path(str(result["destination"])).resolve()
        checkpoint(stage, {"plan_root": str(plan_root)}, {"plan_root": plan_root})
        plan_roots[mode] = plan_root

    recipe_path = root / "preparation" / "staging-recipe.json"
    ledger_checkpoint = reuse("ledger")
    if ledger_checkpoint is not None:
        recipe_path = Path(str(ledger_checkpoint["recipe_path"])).resolve()
        ledger_result = dict(ledger_checkpoint["ledger_result"])
    else:
        release.create_recipe(
            provider_ledger_spec=None,
            provider_ledger_generation=ledger_source,
            baseline_spec=_pin(config["baseline_spec_template"], "baseline spec"),
            base_factor_file=_pin(config["base_factor_file"], "base factor file"),
            implementation_reconciliation=_pin(
                config["implementation_reconciliation"], "implementation reconciliation"
            ),
            implementation_return_attribution=_pin(
                config["implementation_return_attribution"],
                "implementation attribution",
            ),
            strategy_attribution_root=attribution_source.parents[1],
            strategy_attribution_generation_id=attribution_source.name,
            decomposition_plan_bundle=plan_roots["LIVE_ONLY"],
            hybrid_decomposition_plan_bundle=plan_roots["LIVE_PLUS_BACKTEST"],
            correlation_source_root=dependence_root,
            legacy_panel=Path(str(config["legacy_panel_root"])),
            as_of_date=cutoff,
            pipeline_id="qe-attended-native-eod17",
            release_commit=release_commit,
            output=recipe_path,
            epoch_manifest_generation_root=None,
            epoch_manifest_generation_id=None,
            dependence_selected_inputs_path=selected_path,
            source_clock_path=source_clock,
            evaluated_on=evaluated_on,
        )
        ledger_result = release.build_ledger(recipe_path, root / "stages" / "ledger")
        release._record_stage(root, "ledger", ledger_result)  # noqa: SLF001
        checkpoint(
            "ledger",
            {"ledger_result": ledger_result, "recipe_path": str(recipe_path)},
            {
                "ledger_generation": root / "stages" / "ledger",
                "recipe_path": recipe_path,
                "stage_result": root / "stage-results" / "ledger.json",
            },
        )
    baseline_checkpoint = reuse("baseline")
    if baseline_checkpoint is not None:
        baseline_result = dict(baseline_checkpoint["baseline_result"])
    else:
        baseline_result = release.build_baseline(
            recipe_path, root / "stages" / "ledger", root / "stages" / "baseline"
        )
        release._record_stage(root, "baseline", baseline_result)  # noqa: SLF001
        checkpoint(
            "baseline",
            {"baseline_result": baseline_result},
            {
                "baseline_generation": root / "stages" / "baseline",
                "stage_result": root / "stage-results" / "baseline.json",
            },
        )

    crosswalk = root / "preparation" / "current-epoch-crosswalk.json"
    epoch_membership_census = (
        _pin(config["epoch_membership_census"], "epoch membership census")
        if config.get("epoch_membership_census") is not None
        else ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
    )
    epoch_checkpoint = reuse("epoch")
    if epoch_checkpoint is not None:
        epoch = dict(epoch_checkpoint["epoch"])
        epoch_root = Path(str(epoch_checkpoint["epoch_root"])).resolve()
        recipe_path = Path(str(epoch_checkpoint["recipe_path"])).resolve()
        _verify_epoch_calculation_cutoff(
            crosswalk_path=crosswalk, epoch_root=epoch_root, cutoff=cutoff
        )
    else:
        rebind_current_epoch_crosswalk(
            template_path=_pin(config["epoch_crosswalk_template"], "epoch template"),
            baseline_generation_dir=(
                root
                / "stages"
                / "baseline"
                / "generations"
                / str(baseline_result["generation_id"])
            ),
            output_path=crosswalk,
            calculation_cutoff_date=cutoff,
        )
        authority, receipts = issue_static_snapshot_receipts(
            ledger_path=root
            / "stages"
            / "ledger"
            / "generations"
            / str(ledger_result["generation_id"])
            / "provider-ledger.json",
            provider_ledger_manifest_path=root
            / "stages"
            / "ledger"
            / "generations"
            / str(ledger_result["generation_id"])
            / "generation.json",
            baseline_path=root
            / "stages"
            / "baseline"
            / "generations"
            / str(baseline_result["generation_id"])
            / "portfolio-baseline.json",
            crosswalk_path=crosswalk,
            issuer_code_path=Path(__file__),
        )
        receipt_root = root / "preparation" / "epoch-receipts"
        authority_path = receipt_root / "authority.json"
        _write(authority_path, authority)
        receipt_paths: dict[str, Path] = {}
        for artifact_id, receipt in receipts.items():
            path = (
                receipt_root
                / "receipts"
                / f"{hashlib.sha256(artifact_id.encode()).hexdigest()}.json"
            )
            _write(path, receipt)
            receipt_paths[artifact_id] = path
        epoch = stage_current_epoch_manifest(
            provider_ledger_generation_root=root / "stages" / "ledger",
            provider_ledger_generation_id=str(ledger_result["generation_id"]),
            baseline_generation_root=root / "stages" / "baseline",
            baseline_generation_id=str(baseline_result["generation_id"]),
            flow_policy_path=ROOT
            / "contracts"
            / "account-strategy-flow-classification-policy-v1.json",
            crosswalk_path=crosswalk,
            manifest_schema_path=ROOT
            / "schemas"
            / "portfolio"
            / "account-strategy-manifest.schema.json",
            receipt_authority_path=authority_path,
            admission_policy_path=_pin(
                config["epoch_admission_policy"], "epoch admission policy"
            ),
            membership_census_path=epoch_membership_census,
            receipt_paths=receipt_paths,
            output_generation_root=root / "stages" / "epoch",
        )
        epoch_root = (
            root / "stages" / "epoch" / "generations" / str(epoch["generation_id"])
        )
        _verify_epoch_calculation_cutoff(
            crosswalk_path=crosswalk, epoch_root=epoch_root, cutoff=cutoff
        )
        recipe_path = root / "preparation" / "release-recipe.json"
        release.create_recipe(
            provider_ledger_spec=None,
            provider_ledger_generation=ledger_source,
            baseline_spec=_pin(config["baseline_spec_template"], "baseline spec"),
            base_factor_file=_pin(config["base_factor_file"], "base factor file"),
            implementation_reconciliation=_pin(
                config["implementation_reconciliation"], "implementation reconciliation"
            ),
            implementation_return_attribution=_pin(
                config["implementation_return_attribution"],
                "implementation attribution",
            ),
            strategy_attribution_root=attribution_source.parents[1],
            strategy_attribution_generation_id=attribution_source.name,
            decomposition_plan_bundle=plan_roots["LIVE_ONLY"],
            hybrid_decomposition_plan_bundle=plan_roots["LIVE_PLUS_BACKTEST"],
            correlation_source_root=dependence_root,
            legacy_panel=Path(str(config["legacy_panel_root"])),
            as_of_date=cutoff,
            pipeline_id="qe-attended-native-eod17",
            release_commit=release_commit,
            output=recipe_path,
            epoch_manifest_generation_root=root / "stages" / "epoch",
            epoch_manifest_generation_id=str(epoch["generation_id"]),
            dependence_selected_inputs_path=selected_path,
            source_clock_path=source_clock,
            evaluated_on=evaluated_on,
        )
        checkpoint(
            "epoch",
            {
                "epoch": epoch,
                "epoch_root": str(epoch_root),
                "recipe_path": str(recipe_path),
            },
            {
                "crosswalk": crosswalk,
                "epoch_generation": epoch_root,
                "receipt_root": receipt_root,
                "recipe_path": recipe_path,
            },
        )

    calendar_seed_path = _pin(config["calendar_authority"], "calendar authority")
    calendar_seed = _read(calendar_seed_path, "calendar authority")
    current_policy = (ROOT / "contracts" / "portfolio-hwm-policy-v4.json").read_bytes()
    effective_policy = capital.governed_hwm_input_policy(
        current_policy, contract.read_bytes(), str(calendar_seed["hwm_policy_sha256"])
    )
    hwm_calendar = root / "preparation" / "hwm-calendar-authority.json"
    hwm_input = root / "preparation" / "hwm-input-generation.json"
    hwm_result = root / "preparation" / "hwm-governed-result.json"
    hwm_checkpoint = reuse("hwm")
    if hwm_checkpoint is not None:
        hwm_calendar = Path(str(hwm_checkpoint["hwm_calendar"])).resolve()
        hwm_input = Path(str(hwm_checkpoint["hwm_input"])).resolve()
        hwm_result = Path(str(hwm_checkpoint["hwm_result"])).resolve()
        calendar = _read(hwm_calendar, "prepared selected HWM calendar authority")
    else:
        selected_hwm_inputs: dict[str, Any] = {
            "account_manifest_generation_path": epoch_root / "generation.json",
            "account_manifest_admission_policy_path": _pin(
                config["epoch_admission_policy"], "epoch admission policy"
            ),
            "membership_census_path": epoch_membership_census,
            "account_manifest_flow_classification_policy_path": ROOT
            / "contracts"
            / "account-strategy-flow-classification-policy-v1.json",
            "provider_ledger_generation_path": root
            / "stages"
            / "ledger"
            / "generations"
            / str(ledger_result["generation_id"])
            / "generation.json",
            "baseline_generation_path": root
            / "stages"
            / "baseline"
            / "generations"
            / str(baseline_result["generation_id"])
            / "generation.json",
            "financial_contract_path": contract,
            "calendar_authority_path": calendar_seed_path,
            "provider_admission_path": root
            / "stages"
            / "ledger"
            / "generations"
            / str(ledger_result["generation_id"])
            / "admit-or-declare-receipt.json",
            "hwm_policy_path": ROOT / "contracts" / "portfolio-hwm-policy-v4.json",
            "hwm_policy_bytes": effective_policy,
        }
        calendar = hwm_inputs.derive_selected_hwm_calendar_authority(
            **selected_hwm_inputs
        )
        _write(hwm_calendar, calendar)
        hwm_generation = hwm_inputs.build_selected_hwm_inputs(
            **(selected_hwm_inputs | {"calendar_authority_path": hwm_calendar})
        )
        _write(hwm_input, hwm_generation)
        result = compute_governed_as_held_composite_hwm(
            _accepted_intervals(hwm_generation, calendar, cutoff),
            hwm_generation["memberships"],
            hwm_generation["component_periods"],
            policy_bytes=effective_policy,
            product_contract_bytes=contract.read_bytes(),
            performance_policy_bytes=(
                ROOT / "contracts" / "portfolio-performance-policy-v1.json"
            ).read_bytes(),
            legacy_policy_bytes=(
                ROOT / "contracts" / "portfolio-hwm-policy-v3.json"
            ).read_bytes(),
            calendar_authority_bytes=hwm_calendar.read_bytes(),
            input_generation_bytes=hwm_input.read_bytes(),
            input_receipt_bytes=(
                root
                / "stages"
                / "ledger"
                / "generations"
                / str(ledger_result["generation_id"])
                / "admit-or-declare-receipt.json"
            ).read_bytes(),
            calendar_accepted_sha256=str(calendar["authority_evidence_sha256"]),
            evaluated_on=evaluated_on,
            complete_through=cutoff,
            maximum_age_days=_maximum_age_days(_read(contract, "financial contract")),
            assurance_profile="RELEASE_QC",
        )
        _write(hwm_result, hwm_json_safe(result))
        verify_governed_hwm_result(
            _read(hwm_result, "persisted governed HWM result"),
            hwm_input.read_bytes(),
            calendar_bytes=hwm_calendar.read_bytes(),
            input_receipt_bytes=(
                root
                / "stages"
                / "ledger"
                / "generations"
                / str(ledger_result["generation_id"])
                / "admit-or-declare-receipt.json"
            ).read_bytes(),
            policy_bytes=effective_policy,
            product_contract_bytes=contract.read_bytes(),
            performance_policy_bytes=(
                ROOT / "contracts" / "portfolio-performance-policy-v1.json"
            ).read_bytes(),
            legacy_policy_bytes=(
                ROOT / "contracts" / "portfolio-hwm-policy-v3.json"
            ).read_bytes(),
        )
        checkpoint(
            "hwm",
            {
                "hwm_calendar": str(hwm_calendar),
                "hwm_input": str(hwm_input),
                "hwm_result": str(hwm_result),
            },
            {
                "hwm_calendar": hwm_calendar,
                "hwm_input": hwm_input,
                "hwm_result": hwm_result,
            },
        )

    capital_input = root / "preparation" / "capital-input-generation.json"
    capital_result = root / "preparation" / "invested-capital-result.json"
    capital_checkpoint = reuse("capital")
    if capital_checkpoint is not None:
        capital_input = Path(str(capital_checkpoint["capital_input"])).resolve()
        capital_result = Path(str(capital_checkpoint["capital_result"])).resolve()
    else:
        capital_input_value = _capital_input_builder()(
            provider_ledger_generation_root=root
            / "stages"
            / "ledger"
            / "generations"
            / str(ledger_result["generation_id"]),
            epoch_manifest_generation_root=epoch_root,
            provider_ledger_spec_path=_pin(
                config["provider_ledger_spec"], "provider ledger spec"
            ),
            financial_contract_path=contract,
            strategy_registry_path=financial_contract.DEFAULT_REGISTRY,
            account_crosswalk_path=_pin(
                config["capital_account_crosswalk"], "capital crosswalk"
            ),
            requested_as_of=cutoff,
            evaluated_on=evaluated_on,
        )
        # The builder returns the portable generation and this machine's
        # locators for it. The generation is written and hashed; the locators
        # go to a sidecar that is never hashed, published, or copied into the
        # report candidate, so the artifact does not depend on the run root.
        capital_input_value, capital_input_locators = capital_input_value
        _write(capital_input, capital_input_value)
        capital.write_local_sidecar(capital_input, capital_input_locators)
        capital_result_value = capital.build_governed_capital_result(
            capital_input, contract
        )
        capital.verify_governed_capital_result(
            capital_result_value, capital_input, contract
        )
        _write(capital_result, capital_result_value)
        outcomes.verify_selected_capital_ledger(
            capital_input,
            root
            / "stages"
            / "ledger"
            / "generations"
            / str(ledger_result["generation_id"])
            / "generation.json",
        )
        checkpoint(
            "capital",
            {
                "capital_input": str(capital_input),
                "capital_result": str(capital_result),
            },
            {"capital_input": capital_input, "capital_result": capital_result},
        )
    attribution_checkpoint = reuse("attribution")
    if attribution_checkpoint is None:
        attribution_result = release.build_attribution(
            recipe_path, root / "stages" / "attribution"
        )
        release._record_stage(root, "attribution", attribution_result)  # noqa: SLF001
        checkpoint(
            "attribution",
            {"attribution_result": attribution_result},
            {
                "attribution": root / "stages" / "attribution",
                "stage_result": root / "stage-results" / "attribution.json",
            },
        )

    source_run = root / "preparation" / "source-run.json"
    final_checkpoint = reuse("final-handoff")
    if final_checkpoint is not None:
        return _reusable_preparation_handoff(
            root=root, config=config, identity=identity, plan=plan
        )
    attended = {
        "schema_version": 1,
        "release_recipe": _definition(recipe_path),
        "history_generation": _directory_definition(history_source),
        "allocation_generation": _directory_definition(baseline_source),
        "financial_input_contract": _definition(contract),
        "required_stages": [
            "ledger",
            "baseline",
            "attribution",
            "analytics",
            "report",
            "panel-candidate",
            "panel",
        ],
        "predecessor_spec": None,
        "data_clock": plan["data_clock"],
        "source_clock": _definition(source_clock),
    }
    _write(
        source_run,
        {
            "schema_version": 1,
            "as_of_date": cutoff,
            "release_commit": release_commit,
            "artifacts": [],
            "attended": attended,
        },
    )
    financial = {
        "--hwm-governed-result": hwm_result,
        "--hwm-governed-input-generation": hwm_input,
        "--hwm-governed-calendar-authority": hwm_calendar,
        "--hwm-governed-admission-receipt": root
        / "stages"
        / "ledger"
        / "generations"
        / str(ledger_result["generation_id"])
        / "admit-or-declare-receipt.json",
        "--invested-capital-result": capital_result,
        "--invested-capital-input-generation": capital_input,
    }
    handoff = {
        "schema_version": 1,
        "source_run": _definition(source_run),
        "financial_stage_inputs": {
            key: _definition(path) for key, path in sorted(financial.items())
        },
        "source_phase": plan["source_phase"],
        "data_clock": plan["data_clock"],
    }
    handoff_path = root / "preparation-handoff.json"
    _write(handoff_path, handoff)
    result = {
        "source_run": str(source_run),
        "financial_stage_inputs": {
            key: str(path) for key, path in sorted(financial.items())
        },
    }
    checkpoint(
        "final-handoff", result, {"handoff": handoff_path, "source_run": source_run}
    )
    return {
        "source_run": source_run,
        "financial_stage_inputs": financial,
        "data_clock": plan["data_clock"],
    }


def prepare_attended_inputs(
    *,
    preparation_spec_path: Path,
    output_root: Path,
    requested_as_of: str,
    evaluated_on: str,
    release_commit: str,
    resume: bool = False,
    on_create: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Record actual preparation work before using its checkpointed producer path.

    `on_create` runs once, immediately after this call creates the output root, so
    the runner can mark the root as its own before anything else lands in it.
    """

    # Source and input QC deliberately precede output creation and operational
    # attempt observation.  A rejected input must not leave a resumable-looking
    # preparation root behind.
    config, plan = _validated_inputs(
        preparation_spec_path, requested_as_of, evaluated_on
    )
    root = output_root.resolve()
    if root.exists():
        if not resume:
            raise AttendedPreparationError(
                "preparation output already exists; use --resume"
            )
    else:
        root.mkdir(parents=True)
        if on_create is not None:
            on_create(root)
    import portfolio_pipeline_materialize as materializer

    attempt = materializer._attended_start_preparation_attempt(root)  # noqa: SLF001
    try:
        result = _prepare_attended_inputs(
            preparation_spec_path=preparation_spec_path,
            output_root=root,
            requested_as_of=requested_as_of,
            evaluated_on=evaluated_on,
            release_commit=release_commit,
            resume=resume,
            observation_started=True,
            validated=(config, plan),
        )
    except KeyboardInterrupt:
        # The attempt stays open, as for any interruption; the run state says so.
        run_storage.write_run_state(
            root,
            "CANCELLED",
            phase="preparation",
            attempt=attempt,
            reason="interrupted",
        )
        raise
    except Exception as exc:
        materializer._attended_finish_preparation_attempt(  # noqa: SLF001
            root, attempt, "FAILED", reason=str(exc) or type(exc).__name__
        )
        raise
    materializer._attended_finish_preparation_attempt(root, attempt, "PASSED")  # noqa: SLF001
    return result
