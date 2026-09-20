"""Entrypoint coverage for the source-configured attended preparation route."""

from __future__ import annotations

import hashlib
from pathlib import Path
import json
import subprocess
import sys

import pytest

from quant_engine.core.contracts import canonical_json_bytes

import portfolio_attended_preparation as preparation
import portfolio_pipeline_materialize as materializer


@pytest.mark.parametrize("resume", [False, True])
def test_hwm_policy_admission_precedes_history_and_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    """A pinned but unsupported calendar policy fails before preparation work."""
    import portfolio_invested_capital as capital

    contract = tmp_path / "contract.json"
    calendar = tmp_path / "calendar.json"
    selection = tmp_path / "selection.json"
    contract.write_text("{}", encoding="utf-8")
    calendar.write_text(json.dumps({"hwm_policy_sha256": "0" * 64}), encoding="utf-8")
    selection.write_text('{"files":{}}', encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "financial_data_cutoff": "2026-09-17",
                "financial_input_contract": preparation._definition(contract),
                "calendar_authority": preparation._definition(calendar),
                "source_selection": preparation._definition(selection),
                "provider_ledger_generation_root": str(tmp_path / "ledger"),
                "retained_verified_generations": {
                    "history": {"root": str(tmp_path / "history")}
                },
            }
        )
    )
    monkeypatch.setattr(preparation, "_selected_history_authorities", lambda _files: {})
    monkeypatch.setattr(
        materializer, "validate_source_phase", lambda **_kwargs: {"status": "PASS"}
    )

    def history_must_not_start(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("history reached before policy admission")

    monkeypatch.setattr(
        preparation, "_retained_history_requires_rebuild", history_must_not_start
    )
    output = tmp_path / "output"
    with pytest.raises(capital.CapitalValidationError, match="HWM policy"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
            resume=resume,
        )
    assert not output.exists()


@pytest.mark.parametrize("resume", [False, True])
def test_provider_replay_rejects_changed_transitive_authority_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    """A pinned replay spec cannot hide a changed nested authority file."""
    import provider_ledger

    authority = tmp_path / "mutable-authority.json"
    authority.write_text("{}", encoding="utf-8")
    authority_hash = hashlib.sha256(authority.read_bytes()).hexdigest()
    provider_spec = tmp_path / "provider.json"
    provider_spec.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "as_of_date": "2026-09-17",
                "output_root": str(tmp_path / "ledger"),
                "provenance": {},
                "providers": {name: {} for name in provider_ledger._PROVIDER_CATALOGS},
                "strategy_inputs": {},
                "strategy_provider_map": {},
                "financial_mvp": {
                    name + suffix: str(authority)
                    if suffix == "_path"
                    else authority_hash
                    for name in ("contract", "schema", "registry")
                    for suffix in ("_path", "_sha256")
                },
            }
        ),
        encoding="utf-8",
    )
    selection = tmp_path / "selection.json"
    selection.write_text('{"files":{}}', encoding="utf-8")
    spec = tmp_path / "preparation.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "financial_data_cutoff": "2026-09-17",
                "provider_ledger_spec": preparation._definition(provider_spec),
                "provider_ledger_generation_root": str(
                    tmp_path / "ledger" / "generations" / ("a" * 64)
                ),
                "source_selection": preparation._definition(selection),
                "financial_input_contract": preparation._definition(selection),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        preparation, "_validate_hwm_authority_inputs", lambda _config: None
    )
    monkeypatch.setattr(preparation, "_selected_history_authorities", lambda _files: {})

    def must_not_start(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("downstream admission reached before provider replay")

    monkeypatch.setattr(materializer, "validate_source_phase", must_not_start)
    authority.write_text('{"changed":true}', encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(
        provider_ledger.ProviderLedgerError, match="financial MVP contract hash differs"
    ):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
            resume=resume,
        )
    assert not output.exists()


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


def test_resumed_preparation_handoff_starts_first_downstream_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified resumed preparation handoff may initialize its first child."""

    output = tmp_path / "output"
    output.mkdir()
    source_run = tmp_path / "prepared-source-run.json"
    clock = {"selected_financial_cutoff": "2026-09-17"}
    source_run.write_bytes(canonical_json_bytes({"attended": {"data_clock": clock}}))
    recipe = tmp_path / "recipe.json"
    recipe.write_text("{}", encoding="utf-8")
    preflight: dict[str, object] = {
        "financial_inputs": {},
        "financial_stage_inputs": {},
        "portfolio_root": tmp_path / "workspace" / "dataPortfolio",
        "python": Path(sys.executable),
        "qa_dir": None,
        "recipe": recipe,
        "repo_root": Path(materializer.__file__).resolve().parents[1],
        "request_identity": {"fixture": "resumed-preparation-handoff"},
        "required_stages": ("ledger",),
        "retained_cache_root": tmp_path / "cache",
        "source_phase": {"status": "PASS"},
        "source_run": source_run,
        "workspace_root": tmp_path / "workspace",
    }
    (output / "preparation-handoff.json").write_bytes(
        canonical_json_bytes(
            {
                "data_clock": clock,
                "financial_stage_inputs": {},
                "schema_version": 1,
                "source_phase": {"status": "PASS"},
                "source_run": materializer._file_definition(source_run),
            }
        )
    )

    # These are the actual operational observation transitions a killed
    # preparation attempt leaves behind before its verified resumed handoff.
    materializer._attended_start_preparation_attempt(output)
    resumed_attempt = materializer._attended_start_preparation_attempt(output)
    materializer._attended_finish_preparation_attempt(
        output, resumed_attempt, "COMPLETED"
    )

    def prepared(**kwargs: object) -> dict[str, object]:
        assert kwargs["resume"] is True
        return {"financial_stage_inputs": {}, "source_run": source_run}

    calls: list[str] = []

    def first_child(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 91, "", "first child stops here")

    monkeypatch.setattr(preparation, "prepare_attended_inputs", prepared)
    monkeypatch.setattr(materializer, "_git", lambda *_args: "a" * 40)
    monkeypatch.setattr(materializer, "_attended_basic_preflight", lambda _args: {})
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", first_child)

    assert materializer.main(_argv(tmp_path, execute=True, resume=True)) == 91
    assert calls == ["ledger"]
    assert json.loads((output / "attended-run.json").read_text(encoding="utf-8")) == {
        "failed_stage": {"returncode": 91, "role": "ledger"},
        "request_identity": {"fixture": "resumed-preparation-handoff"},
        "schema_version": 2,
    }
    observations = json.loads(
        (output / "attended-execution-observations.json").read_text(encoding="utf-8")
    )
    assert [attempt["status"] for attempt in observations["preparation_attempts"]] == [
        "INCOMPLETE",
        "COMPLETED",
    ]
    assert observations["execution_attempts"][-1]["status"] == "FAILED"


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
                "provider_ledger_spec": pin("provider-spec.json"),
                "source_selection": pin(selection_path.name),
                "provider_ledger_generation_root": str(tmp_path / "ledger"),
                "retained_verified_generations": {
                    "history": {"root": str(tmp_path / "history")}
                },
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
    import provider_ledger

    monkeypatch.setattr(
        provider_ledger,
        "verify_candidate",
        lambda *_args, **_kwargs: {"state": "VERIFIED_STAGED"},
    )

    monkeypatch.setattr(preparation, "_pins", lambda *_args: None)
    monkeypatch.setattr(preparation, "_pin", fake_pin)
    # This test isolates history-authority forwarding; policy admission has
    # its own actual-reader rejection regression above.
    monkeypatch.setattr(
        preparation, "_validate_hwm_authority_inputs", lambda _config: None
    )
    monkeypatch.setattr(
        materializer, "validate_source_phase", lambda **_kwargs: source_phase
    )
    monkeypatch.setattr(
        history,
        "verify_layered_history_generation",
        lambda *_args, **kwargs: history_calls.append(kwargs) or {},
    )
    monkeypatch.setattr(
        plans,
        "build_bundle",
        lambda **kwargs: plan_calls.append(kwargs) or ({}, {}),
    )

    preparation._validated_inputs(config_path, "2026-09-17", "2026-09-19")

    expected = {name: tmp_path / f"{name}.json" for name in authority_names}
    assert [
        {name: call[name] for name in authority_names} for call in history_calls
    ] == [
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
        preparation,
        "_selected_history_authorities",
        lambda _files: {
            "provider_admission_receipt_path": tmp_path / "new-receipt",
            "financial_input_contract_path": tmp_path / "new-contract",
            "portfolio123_version_evidence_path": tmp_path / "new-p123",
        },
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
    assert (
        preparation._retained_history_requires_rebuild(
            tmp_path / "history", tmp_path / "contract.json", {}
        )
        is True
    )
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

    assert (
        preparation._rebuild_layered_history(
            {
                "builder_kwargs": {
                    "as_of_date": "2026-09-17",
                    "contract_path": str(source),
                    "repository_sha": "old",
                }
            },
            tmp_path / "prepared-history",
            "b" * 40,
            source,
            {},
        )
        == destination
    )
    assert built["repository_sha"] == "b" * 40
    assert built["contract_path"] == source
    assert verified == ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]


def test_reusable_preparation_handoff_preserves_verified_complete_output(
    tmp_path: Path,
) -> None:
    """A fully bound handoff remains reusable after its checkpoints validate it."""

    root = tmp_path / "prepared"
    preparation_dir = root / "preparation"
    preparation_dir.mkdir(parents=True)
    data_clock = {"latest_common_eod": "2026-09-17"}
    identity = {
        "financial_data_cutoff": "2026-09-17",
        "release_commit": "a" * 40,
        "schema_version": 1,
    }
    (root / "preparation-input-identity.json").write_bytes(
        preparation.canonical_json_bytes(identity)
    )
    source_run = preparation_dir / "source-run.json"
    source_run.write_bytes(
        preparation.canonical_json_bytes(
            {
                "artifacts": [],
                "as_of_date": "2026-09-17",
                "attended": {"data_clock": data_clock},
                "release_commit": "a" * 40,
                "schema_version": 1,
            }
        )
    )
    financial: dict[str, Path] = {}
    for flag in (
        "--hwm-governed-result",
        "--hwm-governed-input-generation",
        "--hwm-governed-calendar-authority",
        "--hwm-governed-admission-receipt",
        "--invested-capital-result",
        "--invested-capital-input-generation",
    ):
        path = (
            preparation_dir / "hwm-calendar-authority.json"
            if flag == "--hwm-governed-calendar-authority"
            else root / f"{flag[2:]}.json"
        )
        path.write_text("{}", encoding="utf-8")
        financial[flag] = path
    calendar_path = financial["--hwm-governed-calendar-authority"]
    calendar_path.write_bytes(
        preparation.canonical_json_bytes({"authority_evidence_sha256": "c" * 64})
    )
    financial["--hwm-governed-input-generation"].write_bytes(
        preparation.canonical_json_bytes({"calendar_sha256": "c" * 64})
    )
    checkpoint = preparation._checkpoint_path(root, "hwm")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(
        preparation.canonical_json_bytes(
            {
                "outputs": {"hwm_calendar": preparation._definition(calendar_path)},
                "result": {"hwm_calendar": str(calendar_path)},
            }
        )
    )
    handoff = {
        "data_clock": data_clock,
        "financial_stage_inputs": {
            flag: preparation._definition(path)
            for flag, path in sorted(financial.items())
        },
        "schema_version": 1,
        "source_phase": {"status": "PASS"},
        "source_run": preparation._definition(source_run),
    }
    (root / "preparation-handoff.json").write_bytes(
        preparation.canonical_json_bytes(handoff)
    )

    actual = preparation._reusable_preparation_handoff(
        root=root,
        config={
            "calendar_authority": preparation._definition(
                financial["--hwm-governed-calendar-authority"]
            )
        },
        identity=identity,
        plan={"data_clock": data_clock, "source_phase": {"status": "PASS"}},
    )

    assert actual == {
        "data_clock": data_clock,
        "financial_stage_inputs": financial,
        "source_run": source_run,
    }


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

    with pytest.raises(
        preparation.AttendedPreparationError, match="input identity differs"
    ):
        preparation.prepare_attended_inputs(
            preparation_spec_path=tmp_path / "config.json",
            output_root=root,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
            resume=True,
        )
    assert (root / "preparation-input-identity.json").is_file()


def test_preparation_checkpoint_never_accepts_partially_rewritten_output(
    tmp_path: Path,
) -> None:
    """A missing receipt or changed producer file cannot be resumed as complete."""

    root = tmp_path / "prepared"
    output = root / "preparation" / "history"
    output.mkdir(parents=True)
    (output / "selected.json").write_text('{"selected":"verified"}', encoding="utf-8")
    run_identity = {"schema_version": 1, "source": "pinned"}
    full_identity = {
        "code": {
            "attended_preparation": {},
            "layered_history_repository_dependencies": {},
            "target_series": {},
        },
        "policy": {},
    }
    identity = preparation._stage_identity(
        "selected-history-normalization", run_identity, full_identity, {}
    )

    assert (
        preparation._load_checkpoint(
            root=root,
            stage="selected-history-normalization",
            identity=identity,
        )
        is None
    )
    preparation._record_checkpoint(
        root=root,
        stage="selected-history-normalization",
        identity=identity,
        result={"history_source": str(output.parent)},
        outputs={"history_source": output},
    )
    (output / "selected.json").write_text('{"selected":"partial"}', encoding="utf-8")

    assert (
        preparation._load_checkpoint(
            root=root,
            stage="selected-history-normalization",
            identity=identity,
        )
        is None
    )


def test_preparation_checkpoint_dependencies_are_not_inferred_from_stage_order() -> (
    None
):
    """The executable declaration rejects a ledger receipt without all producers."""

    with pytest.raises(
        preparation.AttendedPreparationError,
        match="dependency is incomplete",
    ):
        preparation._stage_identity(
            "ledger", {"schema_version": 1}, {"code": {}, "policy": {}}, {}
        )


def test_preparation_checkpoint_rejects_missing_declared_output(
    tmp_path: Path,
) -> None:
    """A receipt cannot omit a real stage output and still suppress its producer."""

    root = tmp_path / "prepared"
    output = root / "preparation" / "history"
    output.mkdir(parents=True)
    (output / "selected.json").write_text('{"selected":"verified"}', encoding="utf-8")
    identity = preparation._stage_identity(
        "selected-history-normalization",
        {"schema_version": 1},
        {
            "code": {
                "attended_preparation": {},
                "layered_history_repository_dependencies": {},
                "target_series": {},
            },
            "policy": {},
        },
        {},
    )
    checkpoint = (
        root / "preparation" / "checkpoints" / "selected-history-normalization.json"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(
        preparation.canonical_json_bytes(
            {
                "input_identity": identity,
                "outputs": {},
                "result": {"history_source": str(output)},
                "schema_version": 1,
                "stage": "selected-history-normalization",
            }
        )
    )

    with pytest.raises(
        preparation.AttendedPreparationError,
        match="checkpoint output differs",
    ):
        preparation._load_checkpoint(
            root=root,
            stage="selected-history-normalization",
            identity=identity,
        )


def _preparation_function_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    history_rebuild_required: bool = False,
) -> tuple[Path, dict[str, int], dict[str, object]]:
    """Keep checkpoint control flow real while replacing only numerical producers."""

    import portfolio_current_decomposition_plan as plans
    import portfolio_current_dependence as dependence
    import portfolio_financial_outcomes as outcomes
    import portfolio_hwm_selected_inputs as hwm_inputs
    import portfolio_invested_capital as capital
    import portfolio_release_builder as release
    import static_snapshot_coverage_receipts as snapshots
    import stage_current_epoch_manifest as epoch_stage
    from quant_engine.portfolio_ledger import hwm_governed_execution as hwm
    import rebind_current_epoch_crosswalk as rebind

    source_selection = tmp_path / "selection.json"
    source_selection.write_text('{"files":{}}', encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(
        '{"authority_evidence_sha256":"calendar","hwm_policy_sha256":"policy"}',
        encoding="utf-8",
    )
    history_root = tmp_path / "history"
    history_root.mkdir()
    baseline_source = tmp_path / "baseline-source"
    baseline_source.mkdir()
    attribution_source = tmp_path / "attribution-source" / "generations" / ("a" * 64)
    attribution_source.mkdir(parents=True)
    config: dict[str, object] = {
        "base_factor_file": str(tmp_path / "factors.csv"),
        "baseline_spec_template": str(tmp_path / "baseline-spec.json"),
        "calendar_authority": str(calendar),
        "capital_account_crosswalk": str(tmp_path / "capital-crosswalk.json"),
        "dependence_selection_template": str(tmp_path / "dependence.json"),
        "epoch_admission_policy": str(tmp_path / "epoch-policy.json"),
        "epoch_crosswalk_template": str(tmp_path / "crosswalk.json"),
        "financial_input_contract": str(contract),
        "financial_data_cutoff": "2026-09-17",
        "implementation_reconciliation": str(tmp_path / "reconciliation.json"),
        "implementation_return_attribution": str(tmp_path / "return-attribution.json"),
        "legacy_panel_root": str(tmp_path / "legacy-panel"),
        "plans": {
            mode: {
                "base_factor_manifest": str(tmp_path / f"{mode}-base-factor.json"),
                "base_source_template_root": str(tmp_path / f"{mode}-base-source"),
                "base_spec_template": str(tmp_path / f"{mode}-base-spec.json"),
                "catalog": str(tmp_path / f"{mode}-catalog.json"),
                "europe_factors": str(tmp_path / f"{mode}-europe.csv"),
                "plan": str(tmp_path / f"{mode}-plan.json"),
                "public_factor_manifest": str(tmp_path / f"{mode}-public.json"),
                "sector_manifest": str(tmp_path / f"{mode}-sector.json"),
                "sector_returns": str(tmp_path / f"{mode}-sectors.csv"),
                "style_manifest": str(tmp_path / f"{mode}-style.json"),
                "style_returns": str(tmp_path / f"{mode}-style.csv"),
                "us_factors": str(tmp_path / f"{mode}-us.csv"),
                "us_industries": str(tmp_path / f"{mode}-industries.csv"),
            }
            for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
        },
        "provider_ledger_generation_root": str(tmp_path / "ledger-source"),
        "provider_ledger_spec": str(tmp_path / "provider-ledger-spec.json"),
        "retained_verified_generations": {
            "attribution": {"root": str(attribution_source)},
            "baseline": {"root": str(baseline_source)},
            "history": {"root": str(history_root)},
        },
        "source_selection": str(source_selection),
    }
    plan = {
        "data_clock": {"latest_common_eod": "2026-09-17"},
        "history_rebuild_required": history_rebuild_required,
        "source_phase": {"status": "PASS"},
    }
    identity = {
        "code": {
            name: {}
            for name in (
                "attended_preparation",
                "capital_input",
                "current_dependence",
                "current_plan",
                "financial_outcomes",
                "hwm_calendar_authority",
                "hwm_inputs",
                "invested_capital",
                "rebind_current_epoch_crosswalk",
                "release_builder",
                "static_snapshot_coverage_receipts",
                "stage_current_epoch_manifest",
                "target_series",
                "account_manifest_generation",
                "account_strategy_manifest",
                "verify_account_manifest_generation",
                "baseline_producer",
                "core_authority",
                "core_publication",
                "freshness",
                "hwm_execution",
                "hwm_governed",
                "hwm_input_kernel",
                "hwm_kernel",
                "ledger_admission",
                "ledger_hwm_inputs",
                "ledger_models",
                "provider_ledger",
                "layered_history_repository_dependencies",
            )
        },
        "financial_data_cutoff": "2026-09-17",
        "policy": {
            name: {}
            for name in (
                "account_manifest_schema",
                "dependence",
                "flow_classification",
                "hwm_current",
                "hwm_legacy",
                "performance",
                "dependence_schema",
            )
        },
        "release_commit": "a" * 40,
        "schema_version": 1,
        "source_phase": plan["source_phase"],
        "source_selection": preparation._definition(source_selection),
    }
    calls = {
        "baseline": 0,
        "capital": 0,
        "dependence": 0,
        "dependence_verify": 0,
        "hwm": 0,
        "history": 0,
        "ledger": 0,
        "plan": 0,
        "rebind": 0,
    }

    monkeypatch.setattr(preparation, "_validated_inputs", lambda *_args: (config, plan))
    monkeypatch.setattr(
        preparation, "_preparation_input_identity", lambda **_kwargs: identity.copy()
    )
    monkeypatch.setattr(preparation, "_selected_history_authorities", lambda _files: {})
    monkeypatch.setattr(
        preparation,
        "_selected_dependence_handoff",
        lambda **_kwargs: {"schema_version": 1},
    )

    def fixture_pin(value: object, _label: str) -> Path:
        if isinstance(value, dict):
            return Path(str(value["path"]))
        return Path(str(value))

    monkeypatch.setattr(preparation, "_pin", fixture_pin)
    monkeypatch.setattr(preparation, "_maximum_age_days", lambda _contract: 1)

    def write_dependence(output: Path, **_kwargs: object) -> dict[str, str]:
        calls["dependence"] += 1
        output.mkdir(parents=True, exist_ok=True)
        (output / "source.json").write_text("{}", encoding="utf-8")
        return {"destination": str(output)}

    monkeypatch.setattr(dependence, "write_source", write_dependence)

    def verify_dependence(*_args: object, **_kwargs: object) -> None:
        calls["dependence_verify"] += 1

    monkeypatch.setattr(dependence, "verify_packaged_source", verify_dependence)

    def write_bundle(output: Path, **_kwargs: object) -> dict[str, str]:
        calls["plan"] += 1
        output.mkdir(parents=True, exist_ok=True)
        (output / "bundle.json").write_text("{}", encoding="utf-8")
        return {"destination": str(output)}

    monkeypatch.setattr(plans, "write_bundle", write_bundle)
    monkeypatch.setattr(
        release,
        "create_recipe",
        lambda **kwargs: preparation._write(kwargs["output"], {}),
    )

    def staged_generation(output: Path, label: str) -> dict[str, str]:
        generation = output / "generations" / (label * 64)
        generation.mkdir(parents=True, exist_ok=True)
        (generation / "generation.json").write_text("{}", encoding="utf-8")
        if label == "l":
            (generation / "admit-or-declare-receipt.json").write_text(
                "{}", encoding="utf-8"
            )
        return {"generation_id": generation.name}

    def build_ledger(_recipe: Path, output: Path) -> dict[str, str]:
        calls["ledger"] += 1
        return staged_generation(output, "l")

    def build_baseline(_recipe: Path, _ledger: Path, output: Path) -> dict[str, str]:
        calls["baseline"] += 1
        return staged_generation(output, "b")

    monkeypatch.setattr(release, "build_ledger", build_ledger)
    monkeypatch.setattr(release, "build_baseline", build_baseline)
    monkeypatch.setattr(
        release,
        "_record_stage",
        lambda root, role, _result: preparation._write(
            root / "stage-results" / f"{role}.json", {"role": role}
        ),
    )

    def rebind_crosswalk(**kwargs: object) -> None:
        calls["rebind"] += 1
        assert kwargs["calculation_cutoff_date"] == "2026-09-17"
        preparation._write(
            Path(str(kwargs["output_path"])),
            {"calculation_cutoff_date": "2026-09-17"},
        )

    monkeypatch.setattr(rebind, "rebind_current_epoch_crosswalk", rebind_crosswalk)
    monkeypatch.setattr(
        snapshots, "issue_static_snapshot_receipts", lambda **_kwargs: ({}, {})
    )

    def stage_epoch(**kwargs: object) -> dict[str, str]:
        output = (
            Path(str(kwargs["output_generation_root"])) / "generations" / ("e" * 64)
        )
        output.mkdir(parents=True, exist_ok=True)
        (output / "generation.json").write_text("{}", encoding="utf-8")
        cutoff = json.loads(
            Path(str(kwargs["crosswalk_path"])).read_text(encoding="utf-8")
        )["calculation_cutoff_date"]
        manifest = output / "outputs" / "account-strategy-manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(
            json.dumps({"calculation_cutoff_date": cutoff}), encoding="utf-8"
        )
        return {"generation_id": output.name}

    monkeypatch.setattr(epoch_stage, "stage_current_epoch_manifest", stage_epoch)
    monkeypatch.setattr(capital, "governed_hwm_input_policy", lambda *_args: b"policy")
    monkeypatch.setattr(
        hwm_inputs,
        "derive_selected_hwm_calendar_authority",
        lambda **_kwargs: json.loads(calendar.read_bytes()),
        raising=False,
    )
    monkeypatch.setattr(
        hwm_inputs,
        "build_selected_hwm_inputs",
        lambda **_kwargs: {
            "calendar_sha256": "calendar",
            "component_periods": [],
            "memberships": [{"effective_start": "2026-09-16"}],
        },
    )

    def compute_hwm(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["hwm"] += 1
        return {}

    monkeypatch.setattr(hwm, "compute_governed_as_held_composite_hwm", compute_hwm)
    monkeypatch.setattr(
        hwm, "verify_governed_hwm_result", lambda *_args, **_kwargs: None
    )

    def build_capital_input(**_kwargs: object) -> dict[str, object]:
        calls["capital"] += 1
        return {}

    monkeypatch.setattr(
        preparation, "_capital_input_builder", lambda: build_capital_input
    )
    monkeypatch.setattr(capital, "build_governed_capital_result", lambda *_args: {})
    monkeypatch.setattr(capital, "verify_governed_capital_result", lambda *_args: None)
    monkeypatch.setattr(outcomes, "verify_selected_capital_ledger", lambda *_args: None)

    def build_attribution(_recipe: Path, output: Path) -> dict[str, str]:
        output.mkdir(parents=True, exist_ok=True)
        (output / "bundle.json").write_text("{}", encoding="utf-8")
        return {"destination": str(output)}

    monkeypatch.setattr(release, "build_attribution", build_attribution)
    return tmp_path / "preparation-spec.json", calls, identity


def test_hwm_stage_checkpoints_and_reuses_its_selected_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HWM builder must consume its local selected-source calendar, not the seed."""

    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    import portfolio_hwm_selected_inputs as hwm_inputs

    output = tmp_path / "prepared"
    local_calendar = output / "preparation" / "hwm-calendar-authority.json"
    derived_calls = 0

    def derive_selected_calendar(**kwargs: object) -> dict[str, object]:
        nonlocal derived_calls
        derived_calls += 1
        assert Path(str(kwargs["calendar_authority_path"])).name == "calendar.json"
        return {
            "authority_evidence_sha256": "derived-calendar",
            "hwm_policy_sha256": "policy",
            "accepted_source_intervals": [],
            "source_calendars": [],
        }

    def build_hwm(**kwargs: object) -> dict[str, object]:
        assert (
            Path(str(kwargs["calendar_authority_path"])).resolve()
            == local_calendar.resolve()
        )
        assert json.loads(local_calendar.read_bytes())["authority_evidence_sha256"] == (
            "derived-calendar"
        )
        return {
            "calendar_sha256": "derived-calendar",
            "component_periods": [],
            "memberships": [{"effective_start": "2026-09-16"}],
        }

    monkeypatch.setattr(
        hwm_inputs,
        "derive_selected_hwm_calendar_authority",
        derive_selected_calendar,
        raising=False,
    )
    monkeypatch.setattr(hwm_inputs, "build_selected_hwm_inputs", build_hwm)

    result = preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )

    assert derived_calls == 1
    assert result["financial_stage_inputs"]["--hwm-governed-calendar-authority"] == (
        local_calendar
    )
    checkpoint = preparation._read(
        output / "preparation" / "checkpoints" / "hwm.json", "HWM checkpoint"
    )
    assert checkpoint["result"]["hwm_calendar"] == str(local_calendar)
    assert "hwm_calendar" in checkpoint["outputs"]

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )
    assert derived_calls == 1


def test_preparation_persists_exact_hwm_decimals_before_verification_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HWM return types must survive the calculation-to-JSON boundary."""
    from decimal import Decimal
    from quant_engine.portfolio_ledger import hwm_governed_execution as hwm

    spec, calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    expected = {
        "current_index": "101.1234567890123456789012345678901",
        "periods": [{"composite_return": "0.0000000000000000000000000000001234"}],
        "count": 1,
        "available": True,
    }

    def compute(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["hwm"] += 1
        return {
            "current_index": Decimal(expected["current_index"]),
            "periods": [{"composite_return": Decimal("1.234E-31")}],
            "count": 1,
            "available": True,
        }

    verified = []

    def verify(result: object, *_args: object, **_kwargs: object) -> None:
        persisted = json.loads(
            (output / "preparation" / "hwm-governed-result.json").read_bytes()
        )
        assert result == persisted == expected
        verified.append(persisted)

    monkeypatch.setattr(hwm, "compute_governed_as_held_composite_hwm", compute)
    monkeypatch.setattr(hwm, "verify_governed_hwm_result", verify)
    kwargs = dict(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    preparation.prepare_attended_inputs(**kwargs)
    assert verified == [expected]
    checkpoint = output / "preparation" / "checkpoints" / "hwm.json"
    before = checkpoint.read_bytes()
    preparation.prepare_attended_inputs(**kwargs, resume=True)
    assert checkpoint.read_bytes() == before
    assert calls["hwm"] == 1


def test_preparation_resume_skips_completed_dependence_after_plan_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real preparation function resumes after an interrupted later producer."""

    spec, calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    import portfolio_current_decomposition_plan as plans

    original = plans.write_bundle

    def interrupted_plan(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise RuntimeError("interrupt after dependence")

    monkeypatch.setattr(plans, "write_bundle", interrupted_plan)
    with pytest.raises(RuntimeError, match="interrupt after dependence"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
        )
    assert calls["dependence"] == 1

    monkeypatch.setattr(plans, "write_bundle", original)
    result = preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    assert calls["dependence"] == 1
    assert result["data_clock"] == {"latest_common_eod": "2026-09-17"}


def test_epoch_checkpoint_rejects_stale_cutoff_before_hwm_or_capital(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recomputed Sep. 2 epoch must stop before HWM or capital can reuse it."""

    spec, calls, identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    import rebind_current_epoch_crosswalk as rebind

    def stale_rebind(**kwargs: object) -> None:
        calls["rebind"] += 1
        preparation._write(
            Path(str(kwargs["output_path"])),
            {"calculation_cutoff_date": "2026-09-02"},
        )

    monkeypatch.setattr(rebind, "rebind_current_epoch_crosswalk", stale_rebind)
    assert isinstance(identity["code"], dict)
    identity["code"]["rebind_current_epoch_crosswalk"] = {"sha256": "changed"}

    with pytest.raises(
        preparation.AttendedPreparationError, match="epoch calculation cutoff differs"
    ):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
            resume=True,
        )

    assert calls["hwm"] == 1
    assert calls["capital"] == 1
    assert not (output / "preparation" / "checkpoints" / "epoch.json").exists()


def test_epoch_leaf_code_change_rebuilds_epoch_and_dependents_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the rebind leaf invalidates epoch descendants, not prior stages."""

    spec, calls, identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    old_epoch = preparation._read(
        output / "preparation" / "checkpoints" / "epoch.json", "old epoch checkpoint"
    )
    assert isinstance(identity["code"], dict)
    identity["code"]["rebind_current_epoch_crosswalk"] = {"sha256": "changed"}

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    new_epoch = preparation._read(
        output / "preparation" / "checkpoints" / "epoch.json", "new epoch checkpoint"
    )
    assert calls["dependence"] == 1
    assert calls["plan"] == 2
    assert calls["rebind"] == 2
    assert calls["hwm"] == 2
    assert calls["capital"] == 2
    assert old_epoch["input_identity"] != new_epoch["input_identity"]


@pytest.mark.parametrize(
    ("identity_section", "leaf", "expected_calls"),
    [
        (
            "code",
            "baseline_producer",
            {
                "baseline": 2,
                "capital": 2,
                "dependence": 1,
                "dependence_verify": 1,
                "hwm": 2,
                "ledger": 1,
                "plan": 2,
                "rebind": 2,
            },
        ),
        (
            "code",
            "hwm_kernel",
            {
                "baseline": 1,
                "capital": 1,
                "dependence": 1,
                "dependence_verify": 1,
                "hwm": 2,
                "ledger": 1,
                "plan": 2,
                "rebind": 1,
            },
        ),
        (
            "code",
            "hwm_inputs",
            {
                "baseline": 1,
                "capital": 2,
                "dependence": 1,
                "dependence_verify": 1,
                "hwm": 2,
                "ledger": 1,
                "plan": 2,
                "rebind": 1,
            },
        ),
        (
            "policy",
            "dependence_schema",
            {
                "baseline": 2,
                "capital": 2,
                "dependence": 2,
                "dependence_verify": 2,
                "hwm": 2,
                "ledger": 2,
                "plan": 2,
                "rebind": 2,
            },
        ),
        (
            "code",
            "freshness",
            {
                "baseline": 2,
                "capital": 2,
                "dependence": 1,
                "dependence_verify": 1,
                "hwm": 2,
                "ledger": 2,
                "plan": 2,
                "rebind": 2,
            },
        ),
        (
            "policy",
            "hwm_legacy",
            {
                "baseline": 2,
                "capital": 2,
                "dependence": 1,
                "dependence_verify": 1,
                "hwm": 2,
                "ledger": 1,
                "plan": 2,
                "rebind": 2,
            },
        ),
    ],
)
def test_preparation_runtime_leaf_rebuilds_only_its_declared_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_section: str,
    leaf: str,
    expected_calls: dict[str, int],
) -> None:
    """A changed consumed leaf cannot be hidden by a same-HEAD checkpoint hit."""

    spec, calls, identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    old_final = preparation._read(
        output / "preparation" / "checkpoints" / "final-handoff.json",
        "old final checkpoint",
    )
    section = identity[identity_section]
    assert isinstance(section, dict)
    section[leaf] = {"sha256": "changed"}

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    assert {name: calls[name] for name in expected_calls} == expected_calls
    assert (
        preparation._read(
            output / "preparation" / "checkpoints" / "final-handoff.json",
            "new final checkpoint",
        )["input_identity"]
        != old_final["input_identity"]
    )


def test_preparation_resume_reuses_all_valid_fixture_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged complete resume does not repeat numerical fixture producers."""

    spec, calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    initial_calls = dict(calls)

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    assert calls == initial_calls


def test_history_native_dependency_mismatch_rebuilds_once_then_reuses_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Original-history mismatch cannot hide a stale normalized checkpoint."""

    spec, calls, identity = _preparation_function_fixture(
        tmp_path, monkeypatch, history_rebuild_required=True
    )

    def rebuild(
        _selection: object,
        output: Path,
        _release_commit: str,
        _contract: Path,
        _authorities: object,
    ) -> Path:
        calls["history"] += 1
        normalized = output / f"normalized-{calls['history']}"
        normalized.mkdir(parents=True)
        (normalized / "generation.json").write_text("{}", encoding="utf-8")
        return normalized

    monkeypatch.setattr(preparation, "_rebuild_layered_history", rebuild)
    output = tmp_path / "prepared"
    for resume in (False, True):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
            resume=resume,
        )
    assert calls["history"] == 1

    code = identity["code"]
    assert isinstance(code, dict)
    code["layered_history_repository_dependencies"] = {"sha256": "changed"}
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )
    assert calls["history"] == 2

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )
    assert calls["history"] == 2


def test_preparation_resume_archives_partially_written_dependence_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A producer that stops before its receipt is never accepted as complete."""

    spec, calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    import portfolio_current_dependence as dependence

    original = dependence.write_source

    def partial_dependence(destination: Path, **_kwargs: object) -> dict[str, str]:
        calls["dependence"] += 1
        destination.mkdir(parents=True)
        (destination / "partial.json").write_text("partial", encoding="utf-8")
        raise RuntimeError("interrupt during dependence")

    monkeypatch.setattr(dependence, "write_source", partial_dependence)
    with pytest.raises(RuntimeError, match="interrupt during dependence"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
        )
    assert calls["dependence"] == 1

    monkeypatch.setattr(dependence, "write_source", original)
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    assert calls["dependence"] == 2
    assert any(
        path.read_text(encoding="utf-8") == "partial"
        for path in (output / "preparation" / "partial" / "dependence").rglob(
            "partial.json"
        )
    )


def test_preparation_full_handoff_rejects_changed_hwm_policy_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full-handoff reuse cannot bypass a current HWM policy identity change."""

    spec, _calls, identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    old_hwm = preparation._read(
        output / "preparation" / "checkpoints" / "hwm.json", "old HWM checkpoint"
    )
    old_final = preparation._read(
        output / "preparation" / "checkpoints" / "final-handoff.json",
        "old final checkpoint",
    )
    assert isinstance(identity["policy"], dict)
    identity["policy"]["performance"] = {"sha256": "changed"}

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    new_hwm = preparation._read(
        output / "preparation" / "checkpoints" / "hwm.json", "new HWM checkpoint"
    )
    new_final = preparation._read(
        output / "preparation" / "checkpoints" / "final-handoff.json",
        "new final checkpoint",
    )
    assert new_hwm["input_identity"] != old_hwm["input_identity"]
    assert new_final["input_identity"] != old_final["input_identity"]


def test_preparation_forwards_evaluation_to_each_release_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Using the financial cutoff for freshness would drop the preparation clock."""

    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    import portfolio_release_builder as release

    recipes: list[dict[str, object]] = []

    def create_recipe(**kwargs: object) -> None:
        recipes.append(dict(kwargs))
        preparation._write(Path(str(kwargs["output"])), {})

    monkeypatch.setattr(release, "create_recipe", create_recipe)

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=tmp_path / "prepared",
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )

    assert len(recipes) == 2
    assert all(recipe["as_of_date"] == "2026-09-17" for recipe in recipes)
    assert all(recipe["evaluated_on"] == "2026-09-19" for recipe in recipes)


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
                    "portfolio123": row(
                        "2024-01-01", "FINITE_SELECTED_MANIFEST_BOUNDARY"
                    ),
                    "quantbase": row(
                        "2026-09-17", "CONVERSION_RECEIPT_POLICY_SELECTED_NATIVE_LABEL"
                    ),
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
    assert all(
        row["precision"] == "DATE" and "timezone" not in row
        for row in clock["source_data_as_of"]
    )


def test_source_clock_recovers_schwab_snapshot_from_pinned_original_observation(
    tmp_path: Path,
) -> None:
    """Normalized admission rows cannot replace the selected native locator."""

    generation_id = "a" * 64
    root = tmp_path / "schwab"
    generation = root / "generations" / generation_id
    generation.mkdir(parents=True)
    catalog = {
        "position_snapshot_as_of": {"observed_at": "2026-09-18T03:59:40.004417+00:00"}
    }
    catalog_path = generation / "schwab-generation.json"
    catalog_path.write_bytes(canonical_json_bytes(catalog))
    manifest_path = generation / "generation.json"
    manifest_path.write_bytes(canonical_json_bytes({"generation_id": generation_id}))

    original = {
        "schema_version": 1,
        "policy_contract_sha256": "b" * 64,
        "financial_cutoff": "2026-09-17",
        "captured_at_utc": "2026-09-19T09:19:17.369302+00:00",
        "providers": {
            "schwab": {
                "root": str(root),
                "generation_id": generation_id,
                "manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
                "qualification": "STAGED_COMPLETE",
                "generation_as_of_date": "2026-09-18",
                "finalized_through": "2026-09-17",
                "finality_evidence": {
                    "path": "unused",
                    "sha256": "c" * 64,
                    "size_bytes": 1,
                },
            }
        },
    }
    original_path = tmp_path / "run-observation.json"
    original_path.write_bytes(canonical_json_bytes(original))
    source_provenance = {
        "captured_at_utc": original["captured_at_utc"],
        "financial_cutoff": original["financial_cutoff"],
        "path": str(original_path),
        "policy_contract_sha256": original["policy_contract_sha256"],
        "providers": [
            {
                "provider_id": "schwab",
                "catalog_sha256": original["providers"]["schwab"]["catalog_sha256"],
                "generation_id": generation_id,
                "manifest_sha256": original["providers"]["schwab"]["manifest_sha256"],
                "qualification": "STAGED_COMPLETE",
                "finalized_through": "2026-09-17",
                "finality_interpretation": {
                    "basis": "CAPTURED_NATIVE_HISTORICAL_NAV_LABEL",
                    "provider_finality_guarantee": False,
                },
            }
        ],
        "sha256": hashlib.sha256(original_path.read_bytes()).hexdigest(),
        "size_bytes": len(original_path.read_bytes()),
    }

    clock = preparation._source_clock(
        {"source_provenance": source_provenance}, "2026-09-17"
    )

    assert clock["source_data_as_of"] == [
        {
            "applies_to_latest_common_eod": True,
            "finality": {
                "basis": "CAPTURED_NATIVE_HISTORICAL_NAV_LABEL",
                "provider_finality_guarantee": False,
            },
            "native_observed_at": "2026-09-18T03:59:40.004417+00:00",
            "precision": "TIMESTAMP",
            "provider_data_date": "2026-09-17",
            "source_id": "schwab",
            "timezone": "America/New_York",
        }
    ]


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
                "freshness": {
                    "max_age_days": 0,
                    "rule": "REVALIDATE_ON_PRODUCT_VERSION_CHANGE",
                },
            },
            {
                "source_id": "live-a",
                "product_requirement": "REQUIRED",
                "capture_status": "CAPTURED_GOVERNED",
                "existence_status": "EXISTS",
                "consumers": ["AS_HELD_COMPOSITE_HWM"],
                "freshness": {
                    "max_age_days": 3,
                    "rule": "THROUGH_ACCEPTED_LIVE_BOUNDARY",
                },
            },
            {
                "source_id": "live-b",
                "product_requirement": "REQUIRED",
                "capture_status": "CAPTURED_GOVERNED",
                "existence_status": "EXISTS",
                "consumers": ["AS_HELD_COMPOSITE_HWM"],
                "freshness": {
                    "max_age_days": 2,
                    "rule": "THROUGH_ACCEPTED_PROVIDER_BOUNDARY",
                },
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

    def execute(
        actual: object, actual_root: Path, *, resume: bool
    ) -> tuple[int, dict[str, object]]:
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


def test_supported_entrypoint_records_preparation_attempt_before_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparation timing starts before its work and remains outside execution."""

    output = tmp_path / "output"
    source_run = tmp_path / "source-run.json"
    source_run.write_text("{}", encoding="utf-8")
    times = iter(("2026-09-19T12:00:00+00:00", "2026-09-19T12:00:01+00:00"))
    events: list[str] = []
    monkeypatch.setattr(materializer, "_execution_timestamp", lambda: next(times))

    def prepare_inner(**kwargs: object) -> dict[str, object]:
        events.append("prepare")
        receipt = json.loads(
            (
                Path(str(kwargs["output_root"]))
                / "attended-execution-observations.json"
            ).read_text(encoding="utf-8")
        )
        assert receipt["preparation_attempts"] == [
            {
                "attempt": 1,
                "started_at": "2026-09-19T12:00:00+00:00",
                "status": "RUNNING",
            }
        ]
        return {"financial_stage_inputs": {}, "source_run": source_run}

    def preflight(_args: object) -> dict[str, object]:
        events.append("preflight")
        return {"prepared": True}

    def execute(
        actual: object, _root: Path, *, resume: bool
    ) -> tuple[int, dict[str, object]]:
        events.append("execute")
        assert actual == {"prepared": True}
        assert resume is False
        return 0, {"status": "EXECUTED"}

    monkeypatch.setattr(preparation, "_prepare_attended_inputs", prepare_inner)
    monkeypatch.setattr(preparation, "_validated_inputs", lambda *_args: ({}, {}))
    monkeypatch.setattr(materializer, "_git", lambda *_args: "a" * 40)
    monkeypatch.setattr(materializer, "_attended_preflight", preflight)
    monkeypatch.setattr(materializer, "_attended_execute", execute)

    assert materializer.main(_argv(tmp_path, execute=True)) == 0
    receipt = json.loads(
        (output / "attended-execution-observations.json").read_text(encoding="utf-8")
    )
    assert receipt == {
        "execution_attempts": [],
        "preparation_attempts": [
            {
                "attempt": 1,
                "ended_at": "2026-09-19T12:00:01+00:00",
                "started_at": "2026-09-19T12:00:00+00:00",
                "status": "COMPLETED",
            }
        ],
        "schema_version": 2,
    }
    assert events == ["prepare", "preflight", "execute"]


def test_preparation_attempt_resume_marks_interruption_without_terminal_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killed preparation attempts remain incomplete when the supported entry resumes."""

    output = tmp_path / "output"
    times = iter(
        (
            "2026-09-19T12:00:00+00:00",
            "2026-09-19T12:01:00+00:00",
            "2026-09-19T12:01:01+00:00",
        )
    )
    monkeypatch.setattr(materializer, "_execution_timestamp", lambda: next(times))

    def interrupted(**_kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(preparation, "_prepare_attended_inputs", interrupted)
    monkeypatch.setattr(preparation, "_validated_inputs", lambda *_args: ({}, {}))
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare_attended_inputs(
            preparation_spec_path=tmp_path / "config.json",
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
        )

    monkeypatch.setattr(
        preparation,
        "_prepare_attended_inputs",
        lambda **_kwargs: {
            "financial_stage_inputs": {},
            "source_run": tmp_path / "run",
        },
    )
    preparation.prepare_attended_inputs(
        preparation_spec_path=tmp_path / "config.json",
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    attempts = json.loads(
        (output / "attended-execution-observations.json").read_text(encoding="utf-8")
    )["preparation_attempts"]
    assert attempts == [
        {
            "attempt": 1,
            "started_at": "2026-09-19T12:00:00+00:00",
            "status": "INCOMPLETE",
        },
        {
            "attempt": 2,
            "ended_at": "2026-09-19T12:01:01+00:00",
            "started_at": "2026-09-19T12:01:00+00:00",
            "status": "COMPLETED",
        },
    ]


def test_preparation_input_qc_rejection_creates_no_operational_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input QC remains ahead of both numerical work and operational receipts."""

    output = tmp_path / "output"
    monkeypatch.setattr(
        preparation,
        "_validated_inputs",
        lambda *_args: (_ for _ in ()).throw(
            preparation.AttendedPreparationError("source input QC rejected")
        ),
    )

    with pytest.raises(preparation.AttendedPreparationError, match="source input QC"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=tmp_path / "config.json",
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "failure", ["missing source QC", "tampered source QC", "wrong cutoff"]
)
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
