"""Entrypoint coverage for the source-configured attended preparation route."""

from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest

import portfolio_attended_preparation as preparation
import portfolio_pipeline_materialize as materializer


def _argv(tmp_path: Path, *, execute: bool = False, resume: bool = False) -> list[str]:
    (tmp_path / "workspace" / "dataPortfolio").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(exist_ok=True)
    argv = [
        "attended",
        "--repo-root",
        str(Path(materializer.__file__).resolve().parents[1]),
        "--python",
        sys.executable,
        "--workspace-root",
        str(tmp_path / "workspace"),
        "--output-root",
        str(tmp_path / "output"),
        "--retained-cache-root",
        str(tmp_path / "cache"),
        "--requested-as-of",
        "2026-09-17",
        "--evaluated-on",
        "2026-09-19",
        "--preparation-spec",
        str(tmp_path / "native-inputs.json"),
    ]
    if execute:
        argv.append("--execute")
    if resume:
        argv.append("--resume")
    return argv


@pytest.mark.parametrize(
    ("flag", "replacement"),
    (
        ("--repo-root", "not-this-entrypoint"),
        ("--python", "missing-python.exe"),
        ("--workspace-root", "missing-workspace"),
    ),
)
def test_attended_preparation_rejects_invalid_basic_environment_before_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    replacement: str,
) -> None:
    """Source validation must not begin before the runner's basic admission checks."""

    calls: list[object] = []

    def no_plan(**_kwargs: object) -> dict[str, object]:
        calls.append("plan")
        raise AssertionError("invalid environment entered source input QC")

    argv = _argv(tmp_path)
    argv[argv.index(flag) + 1] = str(tmp_path / replacement)
    monkeypatch.setattr(preparation, "plan_attended_inputs", no_plan)

    assert materializer.main(argv) == 1
    assert calls == []


def test_validation_passes_selected_history_authorities_to_every_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained generation carrying authority bindings must be replayed with them."""

    authority_names = (
        "provider_admission_receipt_path",
        "financial_input_contract_path",
        "provider_ledger_manifest_path",
        "provider_ledger_catalog_path",
        "portfolio123_version_evidence_path",
    )
    selection_path = tmp_path / "selected.json"
    selection_path.write_text(
        json.dumps(
            {
                "files": {
                    name: {"path": f"{name}.json", "sha256": "x", "size_bytes": 1}
                    for name in authority_names
                }
            }
        ),
        encoding="utf-8",
    )
    def pin(name: str) -> dict[str, object]:
        return {"path": name, "sha256": "x", "size_bytes": 1}
    modes = {
        mode: {
            key: pin(f"{mode}-{key}.json")
            for key in (
                "plan",
                "catalog",
                "us_factors",
                "europe_factors",
                "style_returns",
                "sector_returns",
                "us_industries",
                "base_spec_template",
                "public_factor_manifest",
                "style_manifest",
                "sector_manifest",
            )
        }
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
    }
    for definition in modes.values():
        definition["base_source_template_root"] = str(tmp_path / "base")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "financial_data_cutoff": "2026-09-17",
                "financial_input_contract": pin("contract.json"),
                "source_selection": pin(selection_path.name),
                "provider_ledger_generation_root": str(tmp_path / "ledger"),
                "retained_verified_generations": {"history": {"root": str(tmp_path / "history")}},
                "plans": modes,
            }
        ),
        encoding="utf-8",
    )

    def fake_pin(value: object, _label: str) -> Path:
        assert isinstance(value, dict)
        return tmp_path / str(value["path"])

    source_phase = {
        "status": "PASS",
        "source_provenance": {
            "providers": {
                "schwab": {
                    "finalized_through": "2026-09-17",
                    "finality_interpretation": {
                        "basis": "CAPTURED_NATIVE_HISTORICAL_NAV_LABEL",
                        "provider_finality_guarantee": False,
                    },
                }
            }
        },
    }
    history_calls: list[dict[str, object]] = []
    plan_calls: list[dict[str, object]] = []
    import portfolio_current_decomposition_plan as plans
    import portfolio_target_series as history

    monkeypatch.setattr(preparation, "_pins", lambda *_args: None)
    monkeypatch.setattr(preparation, "_pin", fake_pin)
    monkeypatch.setattr(
        materializer, "validate_source_phase", lambda **_kwargs: source_phase
    )
    monkeypatch.setattr(
        history,
        "verify_layered_history_generation",
        lambda *_args, **kwargs: history_calls.append(kwargs) or {},
    )
    monkeypatch.setattr(
        plans, "build_bundle", lambda **kwargs: plan_calls.append(kwargs) or ({}, {}),
    )

    preparation._validated_inputs(config_path, "2026-09-17", "2026-09-19")

    expected = {name: tmp_path / f"{name}.json" for name in authority_names}
    assert [{name: call[name] for name in authority_names} for call in history_calls] == [
        expected,
        expected,
    ]
    assert [{name: call[name] for name in authority_names} for call in plan_calls] == [
        expected,
        expected,
    ]


def test_selected_dependence_handoff_replaces_every_stale_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The historical template cannot retain a superseded ledger authority."""

    template = {
        "schema_version": 1,
        "history_generation_root": "old-history",
        "allocation_generation_root": "old-baseline",
        "provider_ledger_generation_root": "old-ledger",
        "financial_input_contract_path": "old-contract",
        "provider_admission_receipt_path": "old-receipt",
        "portfolio123_version_evidence_path": "old-p123",
    }
    selection = {
        "files": {
            "provider_admission_receipt_path": {"path": "new-receipt"},
            "financial_input_contract_path": {"path": "new-contract"},
            "portfolio123_version_evidence_path": {"path": "new-p123"},
        }
    }
    config = {
        "provider_ledger_generation_root": str(tmp_path / "new-ledger"),
        "dependence_selection_template": {"path": "template.json"},
    }

    monkeypatch.setattr(preparation, "_read", lambda *_args: template.copy())
    monkeypatch.setattr(
        preparation, "_pin", lambda _value, _label: tmp_path / "template.json"
    )
    monkeypatch.setattr(
        preparation, "_selected_history_authorities", lambda _files: {
            "provider_admission_receipt_path": tmp_path / "new-receipt",
            "financial_input_contract_path": tmp_path / "new-contract",
            "portfolio123_version_evidence_path": tmp_path / "new-p123",
        }
    )

    actual = preparation._selected_dependence_handoff(
        config=config,
        selection=selection,
        history_source=tmp_path / "new-history",
        baseline_source=tmp_path / "new-baseline",
        contract=tmp_path / "new-contract",
    )

    assert actual == {
        "schema_version": 1,
        "history_generation_root": str((tmp_path / "new-history").resolve()),
        "allocation_generation_root": str((tmp_path / "new-baseline").resolve()),
        "provider_ledger_generation_root": str((tmp_path / "new-ledger").resolve()),
        "financial_input_contract_path": str((tmp_path / "new-contract").resolve()),
        "provider_admission_receipt_path": str((tmp_path / "new-receipt").resolve()),
        "portfolio123_version_evidence_path": str((tmp_path / "new-p123").resolve()),
    }


def test_attended_basic_preflight_probes_configured_runtime_before_source_qc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file-shaped --python argument is not enough to start preparation."""

    calls: list[Path] = []
    monkeypatch.setattr(
        materializer,
        "_runtime_identity",
        lambda python, repo: calls.append(python) or {"repo_root": str(repo)},
    )

    args = materializer._parser().parse_args(_argv(tmp_path))
    basic = materializer._attended_basic_preflight(args)

    assert basic["python"] == Path(sys.executable).resolve()
    assert calls == [Path(sys.executable).resolve()]


def test_history_dependency_mismatch_requires_rebuild_but_source_failure_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a trusted-code mismatch may replace a retained derived generation."""

    import portfolio_target_series as history

    calls: list[str] = []

    def changed(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["require_mode"]))
        raise history.TargetSeriesError(
            "layered history repository dependency differs: schemas/portfolio/financial-mvp-source-contract.schema.json"
        )

    monkeypatch.setattr(history, "verify_layered_history_generation", changed)
    assert preparation._retained_history_requires_rebuild(
        tmp_path / "history", tmp_path / "contract.json", {}
    ) is True
    assert calls == ["LIVE_ONLY"]

    def corrupted(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise history.TargetSeriesError("layered history artifact sha256 differs")

    monkeypatch.setattr(history, "verify_layered_history_generation", corrupted)
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            tmp_path / "history", tmp_path / "contract.json", {}
        )


def test_rebuild_layered_history_uses_selected_builder_kwargs_and_current_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regenerated history is bound to the frozen selection and current code."""

    import portfolio_target_series as history

    source = tmp_path / "native.csv"
    source.write_text("selected source", encoding="utf-8")
    built: dict[str, object] = {}
    verified: list[str] = []

    def build(**kwargs: object) -> tuple[dict[str, bytes], bytes]:
        built.update(kwargs)
        return {"history.csv": b"economic values"}, b'{"generation_id":"a"}'

    destination = tmp_path / "prepared-history" / "a"
    monkeypatch.setattr(history, "build_layered_history_generation", build)
    monkeypatch.setattr(
        history, "write_layered_history_generation", lambda *_args: destination
    )
    monkeypatch.setattr(
        history,
        "verify_layered_history_generation",
        lambda *_args, **kwargs: verified.append(str(kwargs["require_mode"])) or {},
    )

    assert preparation._rebuild_layered_history(
        {"builder_kwargs": {"as_of_date": "2026-09-17", "contract_path": str(source), "repository_sha": "old"}},
        tmp_path / "prepared-history",
        "b" * 40,
        source,
        {},
    ) == destination
    assert built["repository_sha"] == "b" * 40
    assert built["contract_path"] == source
    assert verified == ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]


def test_attended_preparation_resume_reuses_verified_handoff_before_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged complete handoff is returned without starting a producer."""

    root = tmp_path / "prepared"
    root.mkdir()
    identity = {"schema_version": 1}
    plan = {"source_phase": {"status": "PASS"}, "data_clock": {"eod": "2026-09-17"}}
    expected = {
        "source_run": root / "preparation" / "source-run.json",
        "financial_stage_inputs": {},
        "data_clock": plan["data_clock"],
    }
    calls: list[str] = []

    monkeypatch.setattr(preparation, "_validated_inputs", lambda *_args: ({}, plan))
    monkeypatch.setattr(
        preparation, "_preparation_input_identity", lambda **_kwargs: identity
    )
    monkeypatch.setattr(
        preparation,
        "_reusable_preparation_handoff",
        lambda **_kwargs: calls.append("reuse") or expected,
    )
    monkeypatch.setattr(
        preparation,
        "_capital_input_builder",
        lambda: (_ for _ in ()).throw(AssertionError("resume started capital producer")),
    )

    assert preparation.prepare_attended_inputs(
        preparation_spec_path=tmp_path / "config.json",
        output_root=root,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    ) == expected
    assert calls == ["reuse"]


def test_attended_preparation_resume_rejects_changed_identity_before_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed selected inputs preserve partial bytes instead of rebuilding them."""

    root = tmp_path / "partial"
    root.mkdir()
    (root / "preparation-input-identity.json").write_bytes(
        preparation.canonical_json_bytes({"schema_version": 1, "changed": "old"})
    )
    plan = {"source_phase": {"status": "PASS"}, "data_clock": {"eod": "2026-09-17"}}
    monkeypatch.setattr(preparation, "_validated_inputs", lambda *_args: ({}, plan))
    monkeypatch.setattr(
        preparation,
        "_preparation_input_identity",
        lambda **_kwargs: {"schema_version": 1, "changed": "new"},
    )
    monkeypatch.setattr(
        preparation,
        "_capital_input_builder",
        lambda: (_ for _ in ()).throw(AssertionError("changed input rebuilt capital")),
    )

    with pytest.raises(preparation.AttendedPreparationError, match="input identity differs"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=tmp_path / "config.json",
            output_root=root,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
            resume=True,
        )
    assert (root / "preparation-input-identity.json").is_file()


def test_source_clock_excludes_finite_history_from_live_common_eod() -> None:
    """Closed and fixed-history rows remain disclosed but cannot cap live common EOD."""

    def row(day: str, basis: str) -> dict[str, object]:
        return {
            "finalized_through": day,
            "finality_interpretation": {
                "basis": basis,
                "provider_finality_guarantee": False,
            },
        }

    clock = preparation._source_clock(
        {
            "source_provenance": {
                "providers": {
                    "alpaca": row("2025-01-01", "FINITE_SELECTED_MANIFEST_BOUNDARY"),
                    "fidelity": row("2025-02-01", "FINITE_SELECTED_MANIFEST_BOUNDARY"),
                    "ibkr": row("2026-09-18", "NATIVE_FLEX_STATEMENT_LABEL"),
                    "matrixport": row("2026-09-17", "PRIOR_UTC_DAILY_LABEL_SELECTION"),
                    "portfolio123": row("2024-01-01", "FINITE_SELECTED_MANIFEST_BOUNDARY"),
                    "quantbase": row("2026-09-17", "CONVERSION_RECEIPT_POLICY_SELECTED_NATIVE_LABEL"),
                    "schwab": row("2026-09-17", "CAPTURED_NATIVE_HISTORICAL_NAV_LABEL"),
                }
            }
        },
        "2026-09-17",
    )

    assert clock["latest_common_eod"] == "2026-09-17"
    assert clock["limiting_inputs"] == ["matrixport", "quantbase", "schwab"]
    assert {row["source_id"] for row in clock["source_data_as_of"]} == {
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    }


def test_source_clock_rejects_unclassified_selected_finality() -> None:
    with pytest.raises(preparation.AttendedPreparationError, match="finality differs"):
        preparation._source_clock(
            {
                "source_provenance": {
                    "providers": {"schwab": {"finalized_through": "2026-09-17"}}
                }
            },
            "2026-09-17",
        )


def test_hwm_warning_age_uses_required_dated_hwm_sources_only() -> None:
    contract = {
        "source_segments": [
            {
                "source_id": "static-registry",
                "product_requirement": "REQUIRED",
                "capture_status": "CAPTURED_GOVERNED",
                "existence_status": "EXISTS",
                "consumers": ["AS_HELD_COMPOSITE_HWM"],
                "freshness": {"max_age_days": 0, "rule": "REVALIDATE_ON_PRODUCT_VERSION_CHANGE"},
            },
            {
                "source_id": "live-a",
                "product_requirement": "REQUIRED",
                "capture_status": "CAPTURED_GOVERNED",
                "existence_status": "EXISTS",
                "consumers": ["AS_HELD_COMPOSITE_HWM"],
                "freshness": {"max_age_days": 3, "rule": "THROUGH_ACCEPTED_LIVE_BOUNDARY"},
            },
            {
                "source_id": "live-b",
                "product_requirement": "REQUIRED",
                "capture_status": "CAPTURED_GOVERNED",
                "existence_status": "EXISTS",
                "consumers": ["AS_HELD_COMPOSITE_HWM"],
                "freshness": {"max_age_days": 2, "rule": "THROUGH_ACCEPTED_PROVIDER_BOUNDARY"},
            },
        ]
    }

    assert preparation._maximum_age_days(contract) == 2


def test_attended_plan_uses_source_configuration_without_numerical_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing a plan-only validation with preparation must fail this route."""

    calls: list[str] = []
    declared_clock = {
        "latest_common_eod": "2026-09-17",
        "selected_financial_cutoff": "2026-09-17",
        "limiting_inputs": ["schwab"],
        "source_data_as_of": [],
    }

    def plan(**kwargs: object) -> dict[str, object]:
        calls.append("plan")
        assert kwargs["requested_as_of"] == "2026-09-17"
        assert kwargs["evaluated_on"] == "2026-09-19"
        return {"data_clock": declared_clock, "status": "PLANNED"}

    def no_prepare(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("planning may not calculate preparation")

    monkeypatch.setattr(preparation, "plan_attended_inputs", plan)
    monkeypatch.setattr(preparation, "prepare_attended_inputs", no_prepare)

    assert materializer.main(_argv(tmp_path)) == 0
    assert calls == ["plan"]


def test_attended_execute_binds_prepared_handoff_before_native_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing handoff order or dropping governed paths must fail this boundary."""

    output = tmp_path / "output"
    source_run = tmp_path / "prepared-source-run.json"
    source_run.write_text("{}", encoding="utf-8")
    governed = {
        "--hwm-governed-result": tmp_path / "hwm-result.json",
        "--hwm-governed-input-generation": tmp_path / "hwm-input.json",
        "--hwm-governed-calendar-authority": tmp_path / "hwm-calendar.json",
        "--hwm-governed-admission-receipt": tmp_path / "hwm-receipt.json",
        "--invested-capital-result": tmp_path / "capital-result.json",
        "--invested-capital-input-generation": tmp_path / "capital-input.json",
    }
    for path in governed.values():
        path.write_text("{}", encoding="utf-8")
    events: list[str] = []

    def prepare(**kwargs: object) -> dict[str, object]:
        events.append("prepare")
        assert kwargs == {
            "preparation_spec_path": tmp_path / "native-inputs.json",
            "output_root": output,
            "requested_as_of": "2026-09-17",
            "evaluated_on": "2026-09-19",
            "release_commit": "a" * 40,
            "resume": False,
        }
        return {
            "source_run": source_run,
            "financial_stage_inputs": governed,
            "data_clock": {"selected_financial_cutoff": "2026-09-17"},
        }

    preflight: dict[str, object] = {"required_stages": (), "request_identity": {}}

    def check_handoff(args: object) -> dict[str, object]:
        events.append("preflight")
        assert getattr(args, "source_run") == source_run
        for flag, path in governed.items():
            assert getattr(args, flag[2:].replace("-", "_")) == path
        return preflight

    def execute(actual: object, actual_root: Path, *, resume: bool) -> tuple[int, dict[str, object]]:
        events.append("execute")
        assert actual is preflight
        assert actual_root == output
        assert resume is False
        return 0, {"status": "EXECUTED"}

    monkeypatch.setattr(preparation, "prepare_attended_inputs", prepare)
    monkeypatch.setattr(materializer, "_git", lambda *_args: "a" * 40)
    monkeypatch.setattr(materializer, "_attended_preflight", check_handoff)
    monkeypatch.setattr(materializer, "_attended_execute", execute)

    assert materializer.main(_argv(tmp_path, execute=True)) == 0
    assert events == ["prepare", "preflight", "execute"]


@pytest.mark.parametrize("failure", ["missing source QC", "tampered source QC", "wrong cutoff"])
def test_attended_execute_rejects_bad_preparation_before_output_or_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Any rejected preparation input must start neither preparation output nor child."""

    output = tmp_path / "output"

    def reject(**_kwargs: object) -> dict[str, object]:
        raise preparation.AttendedPreparationError(failure)

    def no_preflight(_args: object) -> dict[str, object]:
        raise AssertionError("invalid source preparation entered attended preflight")

    def no_execute(*_args: object, **_kwargs: object) -> tuple[int, dict[str, object]]:
        raise AssertionError("invalid source preparation started a release child")

    monkeypatch.setattr(preparation, "prepare_attended_inputs", reject)
    monkeypatch.setattr(materializer, "_git", lambda *_args: "a" * 40)
    monkeypatch.setattr(materializer, "_attended_preflight", no_preflight)
    monkeypatch.setattr(materializer, "_attended_execute", no_execute)

    assert materializer.main(_argv(tmp_path, execute=True)) == 1
    assert not output.exists()
