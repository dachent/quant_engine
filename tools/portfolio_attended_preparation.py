"""Native retained-input preparation for the supported attended runner.

This module deliberately composes existing source readers and producers.  It is
not a second pipeline: its only output is the schema-1 source-run handoff that
``portfolio_pipeline_materialize attended`` already consumes.
"""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping

from quant_engine.core.contracts import canonical_json_bytes


class AttendedPreparationError(ValueError):
    """Pinned native input QC or preparation handoff is not admissible."""


ROOT = Path(__file__).resolve().parents[1]


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
    if hashlib.sha256(content).hexdigest() != value["sha256"] or len(content) != value["size_bytes"]:
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
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def _directory_definition(path: Path) -> dict[str, object]:
    files = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise AttendedPreparationError("prepared directory contains a link")
        if child.is_file():
            content = child.read_bytes()
            files.append({"path": child.relative_to(path).as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {"path": str(path.resolve()), "file_count": len(files), "inventory_sha256": hashlib.sha256(encoded).hexdigest()}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json_bytes(dict(value))
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise AttendedPreparationError(f"prepared output differs: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)


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
        for key in ("native_observed_at", "observed_at", "captured_at", "published_at", "timezone", "precision"):
            if key in row:
                # Assembly captured_at_utc is intentionally excluded: it is not native data time.
                item["native_observed_at" if key == "observed_at" else key] = row[key]
        position = row.get("position_snapshot_as_of")
        if isinstance(position, Mapping):
            if "observed_at" in position:
                item["native_observed_at"] = position["observed_at"]
            if "provider_data_date" in position:
                item["provider_data_date"] = _date(position["provider_data_date"], "position data date")
            if "timezone" in position:
                item["timezone"] = position["timezone"]
        # The provider row names the selected immutable generation.  Read its
        # native catalog only to recover a stated observation clock; never use
        # the assembled run-observation timestamp as a substitute.
        root = row.get("root")
        generation_id = row.get("generation_id")
        if isinstance(root, str) and isinstance(generation_id, str):
            generation = Path(root) / "generations" / generation_id
            for name in ("schwab-generation.json", "ibkr-catalog.json", "catalog.json"):
                candidate = generation / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    native = json.loads(candidate.read_bytes())
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                snapshot = native.get("position_snapshot_as_of") if isinstance(native, Mapping) else None
                if isinstance(snapshot, Mapping):
                    if "observed_at" in snapshot:
                        item["native_observed_at"] = snapshot["observed_at"]
                    if "provider_data_date" in snapshot:
                        item["provider_data_date"] = _date(snapshot["provider_data_date"], "position data date")
                    if "timezone" in snapshot:
                        item["timezone"] = snapshot["timezone"]
                    break
        rows.append(item)
    if not applicable_final_dates:
        raise AttendedPreparationError("native source current finality is absent")
    latest = min(applicable_final_dates.values())
    limiting = sorted(
        key for key, value in applicable_final_dates.items() if value == latest
    )
    if latest < cutoff:
        raise AttendedPreparationError("native source finality does not support requested cutoff")
    return {
        "latest_common_eod": latest,
        "selected_financial_cutoff": cutoff,
        "limiting_inputs": limiting,
        "source_data_as_of": rows,
    }


def _selected_history_authorities(files: Mapping[str, object]) -> dict[str, Path]:
    """Resolve the retained reader authorities from the selected source record."""

    names = (
        "provider_admission_receipt_path",
        "financial_input_contract_path",
        "provider_ledger_manifest_path",
        "provider_ledger_catalog_path",
        "portfolio123_version_evidence_path",
    )
    return {
        name: _pin(files.get(name), f"selected native {name}") for name in names
    }


def _retained_history_requires_rebuild(
    history_root: Path,
    contract_path: Path,
    authorities: Mapping[str, Path],
) -> bool:
    """Distinguish stale code-bound derived history from invalid source evidence."""

    import portfolio_target_series as history

    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        try:
            history.verify_layered_history_generation(
                history_root, contract_path, require_mode=mode, **authorities
            )
        except history.TargetSeriesError as exc:
            if str(exc).startswith("layered history repository dependency differs:"):
                return True
            raise AttendedPreparationError("retained layered history is invalid") from exc
    return False


def _rebuild_layered_history(
    selection: Mapping[str, object],
    output_root: Path,
    release_commit: str,
    contract_path: Path,
    authorities: Mapping[str, Path],
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
        kwargs[name] = Path(value) if name.endswith("_path") and value is not None else value
    if kwargs.get("contract_path") != contract_path:
        raise AttendedPreparationError("selected history contract differs")
    if not isinstance(kwargs.get("as_of_date"), str):
        raise AttendedPreparationError("selected history cutoff differs")
    kwargs["repository_sha"] = release_commit


    artifacts, manifest = history.build_layered_history_generation(**kwargs)
    rebuilt = history.write_layered_history_generation(output_root, artifacts, manifest)
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        history.verify_layered_history_generation(
            rebuilt, contract_path, require_mode=mode, **authorities
        )
    return rebuilt


def _validated_inputs(preparation_spec_path: Path, requested_as_of: str, evaluated_on: str) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _read(preparation_spec_path.resolve(), "preparation specification")
    if config.get("schema_version") != 1:
        raise AttendedPreparationError("preparation specification schema differs")
    cutoff = _date(config.get("financial_data_cutoff"), "configured financial cutoff")
    if cutoff != _date(requested_as_of, "requested financial cutoff"):
        raise AttendedPreparationError("requested financial cutoff differs from pinned input")
    if _date(evaluated_on, "evaluated_on") < cutoff:
        raise AttendedPreparationError("evaluated_on precedes financial cutoff")
    _pins(config, "preparation specification")
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
        provider_ledger_generation_root=Path(str(config["provider_ledger_generation_root"])),
        financial_input_contract_path=_pin(config["financial_input_contract"], "financial input contract"),
    )
    # Replay the retained history and every public-plan boundary before this
    # function returns.  The caller creates its output root only afterwards.
    import portfolio_current_decomposition_plan as plans

    contract = _pin(config["financial_input_contract"], "financial input contract")
    history_root = Path(str(config["retained_verified_generations"]["history"]["root"])).resolve()
    rebuild_required = _retained_history_requires_rebuild(
        history_root, contract, history_authorities
    )
    if not rebuild_required:
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
            definition = config["plans"][mode]
            market = {
                key: _pin(definition[key], f"{mode} {key}")
                for key in ("us_factors", "europe_factors", "style_returns", "sector_returns", "us_industries")
            }
            plans.build_bundle(
                plan_path=_pin(definition["plan"], f"{mode} plan"),
                catalog_path=_pin(definition["catalog"], f"{mode} catalog"),
                financial_mvp_contract_path=contract,
                layered_history_generation_path=history_root,
                base_spec_path=_pin(definition["base_spec_template"], f"{mode} base spec"),
                base_source_root=Path(str(definition["base_source_template_root"])),
                market_paths=market,
                public_factor_manifest_path=_pin(definition["public_factor_manifest"], f"{mode} public manifest"),
                style_manifest_path=_pin(definition["style_manifest"], f"{mode} style manifest"),
                sector_manifest_path=_pin(definition["sector_manifest"], f"{mode} sector manifest"),
                evaluation_date=evaluated_on,
                evidence_mode=mode,
                assurance_profile="RELEASE_QC",
                **history_authorities,
            )
    return config, {
        "source_phase": phase,
        "data_clock": _source_clock(phase, cutoff),
        "history_rebuild_required": rebuild_required,
    }


def plan_attended_inputs(*, preparation_spec_path: Path, requested_as_of: str, evaluated_on: str) -> dict[str, Any]:
    """Run all source/input admission checks and declare clocks without writes."""
    _config, plan = _validated_inputs(preparation_spec_path, requested_as_of, evaluated_on)
    return {"status": "PLANNED", **plan}


def _accepted_intervals(generation: Mapping[str, Any], calendar: Mapping[str, Any], cutoff: str) -> list[dict[str, str]]:
    memberships = generation.get("memberships")
    authority = calendar.get("authority_evidence_sha256")
    if not isinstance(memberships, list) or not isinstance(authority, str):
        raise AttendedPreparationError("governed HWM input or calendar differs")
    start = min(_date(row.get("effective_start"), "HWM membership start") for row in memberships if isinstance(row, Mapping))
    day = date.fromisoformat(start)
    end = date.fromisoformat(cutoff)
    rows = []
    while day < end:
        next_day = day + timedelta(days=1)
        left, right = day.isoformat(), next_day.isoformat()
        rows.append({"interval_id": f"{left}/{right}", "interval_start": left, "interval_end": right, "interval_evidence_sha256": hashlib.sha256(f"{left}|{right}|{authority}".encode()).hexdigest()})
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


def _preparation_input_identity(
    *,
    preparation_spec_path: Path,
    config: Mapping[str, Any],
    plan: Mapping[str, Any],
    release_commit: str,
) -> dict[str, Any]:
    """Bind every selected input, policy, and local producer code surface."""

    source_selection = _pin(config["source_selection"], "source selection")
    policy_paths = {
        "dependence": ROOT / "contracts" / "portfolio-dependence-policy-v1.json",
        "flow_classification": ROOT
        / "contracts"
        / "account-strategy-flow-classification-policy-v1.json",
        "hwm_current": ROOT / "contracts" / "portfolio-hwm-policy-v4.json",
        "hwm_legacy": ROOT / "contracts" / "portfolio-hwm-policy-v3.json",
        "performance": ROOT / "contracts" / "portfolio-performance-policy-v1.json",
    }
    code_paths = {
        "attended_preparation": Path(__file__),
        "capital_input": ROOT / "ops" / "mvp2-release" / "scripts" / "build_capital_input.py",
        "current_dependence": ROOT / "tools" / "portfolio_current_dependence.py",
        "current_plan": ROOT / "tools" / "portfolio_current_decomposition_plan.py",
        "financial_outcomes": ROOT / "tools" / "portfolio_financial_outcomes.py",
        "hwm_inputs": ROOT / "tools" / "portfolio_hwm_selected_inputs.py",
        "invested_capital": ROOT / "tools" / "portfolio_invested_capital.py",
        "release_builder": ROOT / "tools" / "portfolio_release_builder.py",
        "target_series": ROOT / "tools" / "portfolio_target_series.py",
    }
    return {
        "schema_version": 1,
        "code": {name: _definition(path) for name, path in sorted(code_paths.items())},
        "financial_data_cutoff": config["financial_data_cutoff"],
        "input_config": _definition(preparation_spec_path.resolve()),
        "policy": {name: _definition(path) for name, path in sorted(policy_paths.items())},
        "release_commit": release_commit,
        "source_phase": plan["source_phase"],
        "source_selection": _definition(source_selection),
    }


def _reusable_preparation_handoff(
    *,
    root: Path,
    config: Mapping[str, Any],
    identity: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit only this runner's complete, unchanged private handoff."""

    if _read(root / "preparation-input-identity.json", "preparation input identity") != identity:
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
    local_flags = required_flags - {"--hwm-governed-calendar-authority"}
    # Returned producer outputs must be under this private root. The one
    # external authority is re-derived from the current selected config below.
    for path in (source_run, *(financial[flag] for flag in local_flags)):
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise AttendedPreparationError("prepared handoff escapes its private root") from exc
    if financial["--hwm-governed-calendar-authority"] != _pin(
        config["calendar_authority"], "calendar authority"
    ):
        raise AttendedPreparationError("prepared calendar authority differs")
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


def prepare_attended_inputs(*, preparation_spec_path: Path, output_root: Path, requested_as_of: str, evaluated_on: str, release_commit: str, resume: bool = False) -> dict[str, Any]:
    """Build one verified local preparation handoff using retained native inputs."""
    if len(release_commit) != 40:
        raise AttendedPreparationError("release commit differs")
    config, plan = _validated_inputs(preparation_spec_path, requested_as_of, evaluated_on)
    root = output_root.resolve()
    identity = _preparation_input_identity(
        preparation_spec_path=preparation_spec_path,
        config=config,
        plan=plan,
        release_commit=release_commit,
    )
    if root.exists():
        if not resume:
            raise AttendedPreparationError("preparation output already exists; use --resume")
        return _reusable_preparation_handoff(
            root=root, config=config, identity=identity, plan=plan
        )
    root.mkdir(parents=True)
    _write(root / "preparation-input-identity.json", identity)

    import portfolio_current_dependence as dependence
    import portfolio_current_decomposition_plan as plans
    import portfolio_financial_mvp_contract as financial_contract
    import portfolio_financial_outcomes as outcomes
    import portfolio_hwm_selected_inputs as hwm_inputs
    import portfolio_invested_capital as capital
    import portfolio_release_builder as release
    from quant_engine.portfolio_ledger.hwm_governed_execution import compute_governed_as_held_composite_hwm, verify_governed_hwm_result
    from rebind_current_epoch_crosswalk import rebind_current_epoch_crosswalk
    from stage_current_epoch_manifest import stage_current_epoch_manifest
    from static_snapshot_coverage_receipts import issue_static_snapshot_receipts

    cutoff = _date(requested_as_of, "requested financial cutoff")
    contract = _pin(config["financial_input_contract"], "financial input contract")
    selection = _read(
        _pin(config["source_selection"], "source selection"), "source selection"
    )
    files = selection.get("files")
    if not isinstance(files, Mapping):
        raise AttendedPreparationError("source selection pins are absent")
    history_authorities = _selected_history_authorities(files)
    ledger_source = Path(str(config["provider_ledger_generation_root"])).resolve()
    baseline_source = Path(str(config["retained_verified_generations"]["baseline"]["root"])).resolve()
    attribution_source = Path(str(config["retained_verified_generations"]["attribution"]["root"])).resolve()
    history_source = Path(str(config["retained_verified_generations"]["history"]["root"])).resolve()
    if plan["history_rebuild_required"]:
        history_source = _rebuild_layered_history(
            selection,
            root / "preparation" / "history",
            release_commit,
            contract,
            history_authorities,
        )

    # The recipe is produced from retained accepted readers; all later stages use
    # the real private roots below, never a task-script output path.
    selected = _read(_pin(config["dependence_selection_template"], "dependence selection"), "dependence selection")
    selected.update({"history_generation_root": str(history_source), "allocation_generation_root": str(baseline_source), "financial_input_contract_path": str(contract)})
    selected_path = root / "preparation" / "dependence-selected-inputs.json"
    _write(selected_path, selected)
    dependence_result = dependence.write_source(root / "preparation" / "dependence", contract_path=contract, policy_path=ROOT / "contracts" / "portfolio-dependence-policy-v1.json", history_generation_root=history_source, allocation_generation_root=baseline_source, evaluated_on=date.fromisoformat(evaluated_on), selected_inputs_path=selected_path)
    dependence_root = Path(str(dependence_result["destination"]))
    dependence.verify_packaged_source(dependence_root, contract_path=contract, policy_path=ROOT / "contracts" / "portfolio-dependence-policy-v1.json", selected_inputs_path=selected_path)

    plan_roots: dict[str, Path] = {}
    for mode, definition in config["plans"].items():
        market = {key: _pin(definition[key], f"{mode} {key}") for key in ("us_factors", "europe_factors", "style_returns", "sector_returns", "us_industries")}
        result = plans.write_bundle(root / "preparation" / "plans" / mode.lower(), plan_path=_pin(definition["plan"], f"{mode} plan"), catalog_path=_pin(definition["catalog"], f"{mode} catalog"), financial_mvp_contract_path=contract, layered_history_generation_path=history_source, base_spec_path=_pin(definition["base_spec_template"], f"{mode} base spec"), base_source_root=Path(str(definition["base_source_template_root"])), market_paths=market, public_factor_manifest_path=_pin(definition["public_factor_manifest"], f"{mode} public manifest"), style_manifest_path=_pin(definition["style_manifest"], f"{mode} style manifest"), sector_manifest_path=_pin(definition["sector_manifest"], f"{mode} sector manifest"), evaluation_date=evaluated_on, evidence_mode=mode, assurance_profile="RELEASE_QC", **history_authorities)
        plan_roots[mode] = Path(str(result["destination"]))

    recipe_path = root / "preparation" / "staging-recipe.json"
    release.create_recipe(provider_ledger_spec=None, provider_ledger_generation=ledger_source, baseline_spec=_pin(config["baseline_spec_template"], "baseline spec"), base_factor_file=_pin(config["base_factor_file"], "base factor file"), implementation_reconciliation=_pin(config["implementation_reconciliation"], "implementation reconciliation"), implementation_return_attribution=_pin(config["implementation_return_attribution"], "implementation attribution"), strategy_attribution_root=attribution_source.parents[1], strategy_attribution_generation_id=attribution_source.name, decomposition_plan_bundle=plan_roots["LIVE_ONLY"], hybrid_decomposition_plan_bundle=plan_roots["LIVE_PLUS_BACKTEST"], correlation_source_root=dependence_root, legacy_panel=Path(str(config["legacy_panel_root"])), as_of_date=cutoff, pipeline_id="qe-attended-native-eod17", release_commit=release_commit, output=recipe_path, epoch_manifest_generation_root=None, epoch_manifest_generation_id=None, dependence_selected_inputs_path=selected_path)
    ledger_result = release.build_ledger(recipe_path, root / "stages" / "ledger")
    release._record_stage(root, "ledger", ledger_result)  # noqa: SLF001
    baseline_result = release.build_baseline(recipe_path, root / "stages" / "ledger", root / "stages" / "baseline")
    release._record_stage(root, "baseline", baseline_result)  # noqa: SLF001

    crosswalk = root / "preparation" / "current-epoch-crosswalk.json"
    rebind_current_epoch_crosswalk(template_path=_pin(config["epoch_crosswalk_template"], "epoch template"), baseline_generation_dir=(root / "stages" / "baseline" / "generations" / str(baseline_result["generation_id"])), output_path=crosswalk)
    authority, receipts = issue_static_snapshot_receipts(ledger_path=root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "provider-ledger.json", provider_ledger_manifest_path=root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "generation.json", baseline_path=root / "stages" / "baseline" / "generations" / str(baseline_result["generation_id"]) / "portfolio-baseline.json", crosswalk_path=crosswalk, issuer_code_path=Path(__file__))
    receipt_root = root / "preparation" / "epoch-receipts"
    authority_path = receipt_root / "authority.json"
    _write(authority_path, authority)
    receipt_paths: dict[str, Path] = {}
    for artifact_id, receipt in receipts.items():
        path = receipt_root / "receipts" / f"{hashlib.sha256(artifact_id.encode()).hexdigest()}.json"
        _write(path, receipt)
        receipt_paths[artifact_id] = path
    epoch = stage_current_epoch_manifest(provider_ledger_generation_root=root / "stages" / "ledger", provider_ledger_generation_id=str(ledger_result["generation_id"]), baseline_generation_root=root / "stages" / "baseline", baseline_generation_id=str(baseline_result["generation_id"]), flow_policy_path=ROOT / "contracts" / "account-strategy-flow-classification-policy-v1.json", crosswalk_path=crosswalk, manifest_schema_path=ROOT / "schemas" / "portfolio" / "account-strategy-manifest.schema.json", receipt_authority_path=authority_path, admission_policy_path=_pin(config["epoch_admission_policy"], "epoch admission policy"), receipt_paths=receipt_paths, output_generation_root=root / "stages" / "epoch")
    epoch_root = root / "stages" / "epoch" / "generations" / str(epoch["generation_id"])
    recipe_path = root / "preparation" / "release-recipe.json"
    release.create_recipe(provider_ledger_spec=None, provider_ledger_generation=ledger_source, baseline_spec=_pin(config["baseline_spec_template"], "baseline spec"), base_factor_file=_pin(config["base_factor_file"], "base factor file"), implementation_reconciliation=_pin(config["implementation_reconciliation"], "implementation reconciliation"), implementation_return_attribution=_pin(config["implementation_return_attribution"], "implementation attribution"), strategy_attribution_root=attribution_source.parents[1], strategy_attribution_generation_id=attribution_source.name, decomposition_plan_bundle=plan_roots["LIVE_ONLY"], hybrid_decomposition_plan_bundle=plan_roots["LIVE_PLUS_BACKTEST"], correlation_source_root=dependence_root, legacy_panel=Path(str(config["legacy_panel_root"])), as_of_date=cutoff, pipeline_id="qe-attended-native-eod17", release_commit=release_commit, output=recipe_path, epoch_manifest_generation_root=root / "stages" / "epoch", epoch_manifest_generation_id=str(epoch["generation_id"]), dependence_selected_inputs_path=selected_path)

    calendar_path = _pin(config["calendar_authority"], "calendar authority")
    calendar = _read(calendar_path, "calendar authority")
    current_policy = (ROOT / "contracts" / "portfolio-hwm-policy-v4.json").read_bytes()
    effective_policy = capital.governed_hwm_input_policy(current_policy, contract.read_bytes(), str(calendar["hwm_policy_sha256"]))
    hwm_generation = hwm_inputs.build_selected_hwm_inputs(account_manifest_generation_path=epoch_root / "generation.json", account_manifest_admission_policy_path=_pin(config["epoch_admission_policy"], "epoch admission policy"), account_manifest_flow_classification_policy_path=ROOT / "contracts" / "account-strategy-flow-classification-policy-v1.json", provider_ledger_generation_path=root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "generation.json", baseline_generation_path=root / "stages" / "baseline" / "generations" / str(baseline_result["generation_id"]) / "generation.json", financial_contract_path=contract, calendar_authority_path=calendar_path, provider_admission_path=root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "admit-or-declare-receipt.json", hwm_policy_path=ROOT / "contracts" / "portfolio-hwm-policy-v4.json", hwm_policy_bytes=effective_policy)
    hwm_input = root / "preparation" / "hwm-input-generation.json"
    _write(hwm_input, hwm_generation)
    result = compute_governed_as_held_composite_hwm(_accepted_intervals(hwm_generation, calendar, cutoff), hwm_generation["memberships"], hwm_generation["component_periods"], policy_bytes=effective_policy, product_contract_bytes=contract.read_bytes(), performance_policy_bytes=(ROOT / "contracts" / "portfolio-performance-policy-v1.json").read_bytes(), legacy_policy_bytes=(ROOT / "contracts" / "portfolio-hwm-policy-v3.json").read_bytes(), calendar_authority_bytes=calendar_path.read_bytes(), input_generation_bytes=hwm_input.read_bytes(), input_receipt_bytes=(root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "admit-or-declare-receipt.json").read_bytes(), calendar_accepted_sha256=str(calendar["authority_evidence_sha256"]), evaluated_on=evaluated_on, complete_through=cutoff, maximum_age_days=_maximum_age_days(_read(contract, "financial contract")), assurance_profile="RELEASE_QC")
    hwm_result = root / "preparation" / "hwm-governed-result.json"
    _write(hwm_result, result)
    verify_governed_hwm_result(result, hwm_input.read_bytes(), calendar_bytes=calendar_path.read_bytes(), input_receipt_bytes=(root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "admit-or-declare-receipt.json").read_bytes(), policy_bytes=effective_policy, product_contract_bytes=contract.read_bytes(), performance_policy_bytes=(ROOT / "contracts" / "portfolio-performance-policy-v1.json").read_bytes(), legacy_policy_bytes=(ROOT / "contracts" / "portfolio-hwm-policy-v3.json").read_bytes())

    capital_input_value = _capital_input_builder()(provider_ledger_generation_root=root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]), epoch_manifest_generation_root=epoch_root, provider_ledger_spec_path=_pin(config["provider_ledger_spec"], "provider ledger spec"), financial_contract_path=contract, strategy_registry_path=financial_contract.DEFAULT_REGISTRY, account_crosswalk_path=_pin(config["capital_account_crosswalk"], "capital crosswalk"), requested_as_of=cutoff, evaluated_on=evaluated_on)
    capital_input = root / "preparation" / "capital-input-generation.json"
    _write(capital_input, capital_input_value)
    capital_result_value = capital.build_governed_capital_result(capital_input, contract)
    capital.verify_governed_capital_result(capital_result_value, capital_input, contract)
    capital_result = root / "preparation" / "invested-capital-result.json"
    _write(capital_result, capital_result_value)
    outcomes.verify_selected_capital_ledger(capital_input, root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "generation.json")
    attribution_result = release.build_attribution(recipe_path, root / "stages" / "attribution")
    release._record_stage(root, "attribution", attribution_result)  # noqa: SLF001

    source_run = root / "preparation" / "source-run.json"
    attended = {"schema_version": 1, "release_recipe": _definition(recipe_path), "history_generation": _directory_definition(history_source), "allocation_generation": _directory_definition(root / "stages" / "baseline" / "generations" / str(baseline_result["generation_id"])), "financial_input_contract": _definition(contract), "required_stages": ["ledger", "baseline", "attribution", "analytics", "report", "panel-candidate", "panel"], "predecessor_spec": None, "data_clock": plan["data_clock"]}
    _write(source_run, {"schema_version": 1, "as_of_date": cutoff, "release_commit": release_commit, "artifacts": [], "attended": attended})
    financial = {"--hwm-governed-result": hwm_result, "--hwm-governed-input-generation": hwm_input, "--hwm-governed-calendar-authority": calendar_path, "--hwm-governed-admission-receipt": root / "stages" / "ledger" / "generations" / str(ledger_result["generation_id"]) / "admit-or-declare-receipt.json", "--invested-capital-result": capital_result, "--invested-capital-input-generation": capital_input}
    handoff = {"schema_version": 1, "source_run": _definition(source_run), "financial_stage_inputs": {key: _definition(path) for key, path in sorted(financial.items())}, "source_phase": plan["source_phase"], "data_clock": plan["data_clock"]}
    _write(root / "preparation-handoff.json", handoff)
    return {"source_run": source_run, "financial_stage_inputs": financial, "data_clock": plan["data_clock"]}
