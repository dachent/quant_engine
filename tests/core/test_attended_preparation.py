"""Entrypoint coverage for the source-configured attended preparation route."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal, localcontext
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import json
import subprocess
import sys
from typing import Any, NamedTuple, cast

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    run_storage,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes

import portfolio_attended_preparation as preparation
import portfolio_pipeline_materialize as materializer


HISTORY_TEST_PATH = (
    preparation.ROOT
    / "tests"
    / "portfolio_analytics"
    / "test_portfolio_layered_history.py"
)
HISTORY_TEST_SPEC = importlib.util.spec_from_file_location(
    "portfolio_layered_history_fixture_for_attended_preparation", HISTORY_TEST_PATH
)
if HISTORY_TEST_SPEC is None or HISTORY_TEST_SPEC.loader is None:
    raise RuntimeError("unable to load the shared layered-history fixture module")
history_tests = importlib.util.module_from_spec(HISTORY_TEST_SPEC)
sys.modules[HISTORY_TEST_SPEC.name] = history_tests
HISTORY_TEST_SPEC.loader.exec_module(history_tests)

HISTORY_TOOL_RELATIVE_PATH = "tools/portfolio_target_series.py"


@pytest.mark.parametrize("resume", [False, True])
def test_hwm_policy_admission_precedes_history_and_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    """A pinned but unsupported calendar policy fails before preparation work."""
    import portfolio_invested_capital as capital

    contract = tmp_path / "contract.json"
    calendar = tmp_path / "calendar.json"
    selection = tmp_path / "selection.json"
    contract.write_bytes(
        (
            preparation.ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
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
    # This test is about provider replay, not contract currency, so the pinned
    # contract must be a real copy of the product contract rather than a
    # stand-in: `_validate_product_contract_currency` runs first and would
    # otherwise refuse before provider replay is ever reached.
    contract = tmp_path / "contract.json"
    contract.write_bytes(
        (
            preparation.ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
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
                "financial_input_contract": preparation._definition(contract),
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
        # The run stages no QA evidence; --qa-hold is the declared plan for that.
        argv.extend(["--qa-hold", "--execute"])
    if resume:
        argv.append("--resume")
    return argv


def _capital_inputs(root: Path) -> dict[str, Path]:
    """The governed invested-capital family every attended run now carries."""

    paths = {
        "--invested-capital-result": root / "capital-result.json",
        "--invested-capital-input-generation": root / "capital-generation.json",
    }
    for path in paths.values():
        path.write_text("{}", encoding="utf-8")
    return paths


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
    capital = _capital_inputs(tmp_path)
    preflight: dict[str, object] = {
        "financial_inputs": {},
        "financial_stage_inputs": capital,
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
                "financial_stage_inputs": {
                    flag: materializer._file_definition(path)
                    for flag, path in capital.items()
                },
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
    materializer._attended_finish_preparation_attempt(output, resumed_attempt, "PASSED")

    def prepared(**kwargs: object) -> dict[str, object]:
        assert kwargs["resume"] is True
        return {"financial_stage_inputs": capital, "source_run": source_run}

    calls: list[str] = []

    def first_child(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 91, "", "first child stops here")

    # Execution validates declaratively (pin matrix, source phase) before admitting.
    monkeypatch.setattr(
        preparation,
        "plan_attended_inputs",
        lambda **_kwargs: {"status": "PLANNED", "source_phase": {}},
    )
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
    # The killed attempt is CANCELLED (formerly INCOMPLETE), the resumed one PASSED.
    assert [attempt["status"] for attempt in observations["preparation_attempts"]] == [
        "CANCELLED",
        "PASSED",
    ]
    assert observations["execution_attempts"][-1]["status"] == "FAILED"
    state = json.loads((output / "attended-run-state.json").read_text("utf-8"))
    assert (state["state"], state["pid"]) == ("FAILED", os.getpid())


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
    expected_captures: dict[str, dict[str, object]] = {}
    for mode, definition in modes.items():
        manifest_path = tmp_path / f"{mode}-public_factor_manifest.json"
        manifest_content = preparation.canonical_json_bytes(
            {"acquisition_run": {"run_id": "a" * 64}}
        )
        manifest_path.write_bytes(manifest_content)
        definition["public_factor_manifest"] = pin(manifest_path.name)
        expected_captures[mode] = {
            "schema_version": 1,
            "financial_data_cutoff": "2026-09-17",
            "manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
            "manifest_size_bytes": len(manifest_content),
            "acquisition_run_id": "a" * 64,
        }
    for definition in modes.values():
        definition["base_source_template_root"] = str(tmp_path / "base")
    (tmp_path / "contract.json").write_bytes(
        (
            preparation.ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
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
                    "history": {"root": str(tmp_path / "history")},
                    "attribution": {
                        "root": str(
                            tmp_path / "attribution" / "generations" / ("a" * 64)
                        )
                    },
                },
                "expected_staged_baseline": {
                    "generation_id": "b" * 64,
                    "catalog_sha256": "c" * 64,
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
    import portfolio_release_builder as release
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
    attribution_preflight_calls: list[tuple[Path, object, object]] = []
    monkeypatch.setattr(
        release,
        "_validate_strategy_attribution_expected_baseline_binding",
        lambda root, generation_id, expected: attribution_preflight_calls.append(
            (root, generation_id, expected)
        ),
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

    assert attribution_preflight_calls == [
        (
            tmp_path / "attribution",
            "a" * 64,
            {"generation_id": "b" * 64, "catalog_sha256": "c" * 64},
        )
    ]

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
    assert [call["retained_public_factor_capture"] for call in plan_calls] == [
        expected_captures["LIVE_ONLY"],
        expected_captures["LIVE_PLUS_BACKTEST"],
    ]

    config = json.loads(config_path.read_bytes())
    config.pop("expected_staged_baseline")
    config_path.write_bytes(preparation.canonical_json_bytes(config))
    prior_history_calls = len(history_calls)
    prior_plan_calls = len(plan_calls)
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="expected staged baseline identity is required",
    ):
        preparation._validated_inputs(config_path, "2026-09-17", "2026-09-19")
    assert len(history_calls) == prior_history_calls
    assert len(plan_calls) == prior_plan_calls


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


def _install_verifier_history_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the shared synthetic contract parse on both module copies.

    The layered-history fixture module imports ``portfolio_target_series``
    under its own private module name, while ``portfolio_attended_preparation``
    resolves the normally imported module; the sanctioned synthetic producer
    parse must cover both or the fixture builds against a different module
    than the verifier the rebuild decision drives.
    """

    import portfolio_target_series as history

    history_tests._install_synthetic_history_authority(monkeypatch)
    history_tests._install_synthetic_history_authority(monkeypatch, history)


def _install_clean_history_tool_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind HEAD's committed tool bytes to the on-disk bytes: a clean tree.

    The staleness rebuild decision refuses to rebuild unless the executing
    history tool's bytes match HEAD's committed bytes. Pinning the
    committed-byte reader to the on-disk file simulates the clean tree a
    rebuild is licensed for while keeping every path, hash, and comparison in
    the real gate.
    """

    monkeypatch.setattr(
        preparation,
        "_head_committed_file_bytes",
        lambda relative_path: preparation.ROOT.joinpath(
            *relative_path.split("/")
        ).read_bytes(),
    )


def _install_gated_synthetic_history_parse(
    monkeypatch: pytest.MonkeyPatch, history: Any
) -> None:
    """Install the synthetic contract parse behind the real dependency gate."""

    def gated_synthetic_parse(
        path: Path, repository_sha: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        history._require_trusted_layered_history_dependencies(repository_sha)
        return cast(
            "tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]",
            history_tests._synthetic_history_contract(path),
        )

    history_tests._install_synthetic_history_authority(monkeypatch)
    monkeypatch.setattr(history, "_history_contract", gated_synthetic_parse)


def _retained_layered_history(
    tmp_path: Path,
    *,
    fixture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build one real retained generation through the real builder and writer."""

    fixture = (
        fixture if fixture is not None else history_tests._history_fixture(tmp_path)
    )
    artifacts, manifest_content = history_tests._build(fixture)
    generation = history_tests.write_layered_history_generation(
        tmp_path / "retained-history", artifacts, manifest_content
    )
    return fixture, generation


def _generation_recording_code_identity(
    generation: Path,
    output_root: Path,
    *,
    repository_sha: str,
    tool_sha256: str,
) -> Path:
    """Rewrite a retained generation to record another commit and tool hash.

    The rewritten manifest stays canonical and self-consistent: the two
    recorded code-identity fields change, every other byte is kept, and the
    generation identity is recomputed. The verifier's own classification of
    the recorded identity is therefore the only thing that decides the
    verdict.
    """

    artifacts = {
        path.name: path.read_bytes()
        for path in generation.iterdir()
        if path.name != "generation.json"
    }
    manifest = json.loads((generation / "generation.json").read_bytes())
    manifest["repository_sha"] = repository_sha
    manifest["tool_sha256"] = tool_sha256
    return cast(
        Path,
        history_tests.write_layered_history_generation(
            output_root, artifacts, history_tests._reidentify_manifest(manifest)
        ),
    )


def _spy_on_git_show_lookups(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ``git show`` invocation while delegating to the real run."""

    real_run = subprocess.run
    lookups: list[list[str]] = []

    def spying_run(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if "show" in args:
            lookups.append([str(part) for part in args])
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", spying_run)
    return lookups


def _looked_up_tool_revisions(lookups: list[list[str]]) -> list[str]:
    """The ``<revision>:<tool path>`` operand of every recorded tool lookup."""

    return [
        part
        for lookup in lookups
        for part in lookup
        if part.endswith(f":{HISTORY_TOOL_RELATIVE_PATH}")
    ]


def test_stale_repository_dependency_kind_rebuilds_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded repository dependency that moved on is staleness, not corruption.

    The verifier keeps its real trusted-dependency gate: the retained
    generation records the repository SHA it was built at, and when a
    dependency committed at that recorded SHA no longer matches the code that
    defines the history, the retained generation is one code cycle behind
    and is rebuilt rather than failed closed.
    """

    import portfolio_target_series as history

    def gated_synthetic_parse(
        path: Path, repository_sha: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        history._require_trusted_layered_history_dependencies(repository_sha)
        return cast(
            "tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]",
            history_tests._synthetic_history_contract(path),
        )

    history_tests._install_synthetic_history_authority(monkeypatch)
    monkeypatch.setattr(history, "_history_contract", gated_synthetic_parse)

    fixture, generation = _retained_layered_history(tmp_path)
    assert (
        history.verify_layered_history_generation(generation, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )

    committed = {
        relative_path: hashlib.sha256(
            preparation.ROOT.joinpath(*relative_path.split("/")).read_bytes()
        ).hexdigest()
        for relative_path in history.LAYERED_HISTORY_REPOSITORY_DEPENDENCIES
    }
    committed["tools/portfolio_financial_mvp_contract.py"] = "0" * 64
    monkeypatch.setattr(
        history,
        "_committed_layered_history_dependency_sha256s",
        lambda _repository_sha=None: committed,
    )

    with pytest.raises(
        history.TargetSeriesError,
        match="layered history repository dependency differs",
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "staleness"
    _install_clean_history_tool_tree(monkeypatch)
    assert (
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
        is True
    )


def test_stale_tool_identity_kind_rebuilds_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained generation built under a previous tool identity rebuilds.

    The trusted tool SHA comparison is the only detector for an edit to the
    layered-history tool itself: ``portfolio_target_series.py`` is not a
    recorded repository dependency, and this check fires before the
    dependency gate. An edit to the tool therefore leaves every retained
    generation one code cycle behind by design, so the verifier marks it
    ``staleness`` and the rebuild decision rebuilds it instead of failing
    closed. The verifier's real trusted-identity gate stays live: the
    retained manifest below records the genuine tool SHA of the current
    file, and the simulation replaces the trusted identity with a later
    one, exactly what editing this very tool produces after the build.
    The committed-tool lookup below pins the recorded commit to exactly
    the building tool's own bytes — the legitimate-revision case — so
    the verdict stays deterministic even on a dirty working tree. That
    seam hides the real git resolution of the recorded commit, which the
    recorded-ancestor tests further below exercise with no seam pinned.
    """

    import portfolio_target_series as history

    def gated_synthetic_parse(
        path: Path, repository_sha: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        history._require_trusted_layered_history_dependencies(repository_sha)
        return cast(
            "tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]",
            history_tests._synthetic_history_contract(path),
        )

    history_tests._install_synthetic_history_authority(monkeypatch)
    monkeypatch.setattr(history, "_history_contract", gated_synthetic_parse)

    fixture, generation = _retained_layered_history(tmp_path)
    assert (
        history.verify_layered_history_generation(generation, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )

    monkeypatch.setattr(
        history,
        "_trusted_layered_history_code_identity",
        lambda: ("0" * 40, "f" * 64),
    )
    monkeypatch.setattr(
        history,
        "_committed_layered_history_tool_sha256",
        lambda _repository_sha: hashlib.sha256(
            Path(history.__file__).read_bytes()
        ).hexdigest(),
    )

    with pytest.raises(
        history.TargetSeriesError,
        match="layered history code identity is not trusted",
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "staleness"
    _install_clean_history_tool_tree(monkeypatch)
    assert (
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
        is True
    )


def test_fabricated_tool_identity_fails_closed_as_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internally consistent forged tool identity is corruption, not staleness.

    The retained manifest below is canonical, self-consistent, and carries a
    recomputed valid generation identity and the real recorded repository
    SHA, but its ``tool_sha256`` matches no tool bytes that recorded commit
    holds. The verifier must prove a differing recorded identity against
    that commit before classifying it as repairable staleness; an
    unprovable identity fails closed and the rebuild decision never
    converts it into a rebuild or probes the executing tool tree.
    """

    import portfolio_target_series as history

    _install_gated_synthetic_history_parse(monkeypatch, history)
    fixture, generation = _retained_layered_history(tmp_path)

    artifacts = {
        name: (generation / name).read_bytes()
        for name in history.LAYERED_HISTORY_OUTPUT_NAMES
    }
    forged = json.loads((generation / "generation.json").read_bytes())
    forged["tool_sha256"] = "0" * 64
    forged_generation = history_tests.write_layered_history_generation(
        tmp_path / "forged-history",
        artifacts,
        history_tests._reidentify_manifest(forged),
    )
    monkeypatch.setattr(
        history, "_committed_layered_history_tool_sha256", lambda _sha: "1" * 64
    )

    with pytest.raises(
        history.TargetSeriesError,
        match="layered history code identity is not trusted",
    ) as raised:
        history.verify_layered_history_generation(
            forged_generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "integrity"

    def history_tool_tree_must_not_be_probed(_history: Any) -> None:
        raise AssertionError("integrity failure must not reach the rebuild gate")

    monkeypatch.setattr(
        preparation,
        "_require_executing_history_tool_matches_head",
        history_tool_tree_must_not_be_probed,
    )
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            forged_generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )


def test_fabricated_tool_identity_fails_closed_against_the_real_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged tool identity is refused against the recorded commit itself.

    Unlike the seam-pinned variant above, this test keeps the real
    committed-tool resolution live: the retained manifest records the
    checkout's own HEAD as its repository SHA, so the recorded commit
    provably holds tool bytes whose hash differs from the forged
    ``tool_sha256``. The rebuild decision must fail closed as an
    integrity violation, and the recorded commit must actually have been
    consulted -- a verdict reached without the lookup binds nothing. The
    lookup is pinned to the manifest's recorded repository SHA itself,
    read back from the retained generation on disk, so a lookup against
    the literal ``HEAD`` or against a wrong tool path fails here. The
    recorded SHA is HEAD in this checkout, so distinguishing a recorded
    non-HEAD commit from HEAD is the job of the recorded-ancestor tests
    below, which run the same resolution against a real earlier commit.
    """

    import portfolio_target_series as history

    _install_gated_synthetic_history_parse(monkeypatch, history)
    fixture, generation = _retained_layered_history(tmp_path)

    artifacts = {
        name: (generation / name).read_bytes()
        for name in history.LAYERED_HISTORY_OUTPUT_NAMES
    }
    forged = json.loads((generation / "generation.json").read_bytes())
    forged["tool_sha256"] = "0" * 64
    forged_generation = history_tests.write_layered_history_generation(
        tmp_path / "forged-history",
        artifacts,
        history_tests._reidentify_manifest(forged),
    )

    real_run = subprocess.run
    tool_lookups: list[list[str]] = []

    def spying_run(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if "show" in args and any(
            "portfolio_target_series.py" in str(part) for part in args
        ):
            tool_lookups.append([str(part) for part in args])
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(history.subprocess, "run", spying_run)

    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ) as decision:
        preparation._retained_history_requires_rebuild(
            forged_generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
    assert isinstance(decision.value.__cause__, history.TargetSeriesError)
    assert decision.value.__cause__.kind == "integrity"
    assert tool_lookups, "the recorded commit was never consulted"
    recorded_repository_sha = json.loads(
        (forged_generation / "generation.json").read_bytes()
    )["repository_sha"]
    assert _looked_up_tool_revisions(tool_lookups) == [
        f"{recorded_repository_sha}:{HISTORY_TOOL_RELATIVE_PATH}"
    ]


def test_trusted_tool_identity_never_consults_the_recorded_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current retained generation verifies with no committed-tool lookup.

    The recorded-commit tool comparison exists only on the identity
    mismatch path; a generation whose recorded tool identity already
    matches the executing tool must verify VALID without resolving
    committed tool bytes, keeping the correct variant byte-identical in
    behavior. The synthetic fixture's generation is LIVE_ONLY-ready only,
    so the rebuild decision's LIVE_PLUS_BACKTEST leg fails closed on
    that pre-existing mode gap with the lookup sentinel still installed:
    the identity gate contributed no lookup and no rebuild verdict.
    """

    import portfolio_target_series as history

    _install_gated_synthetic_history_parse(monkeypatch, history)
    fixture, generation = _retained_layered_history(tmp_path)

    def commit_lookup_must_not_run(_repository_sha: str) -> str:
        raise AssertionError("trusted tool identity requires no commit lookup")

    monkeypatch.setattr(
        history,
        "_committed_layered_history_tool_sha256",
        commit_lookup_must_not_run,
    )

    assert (
        history.verify_layered_history_generation(generation, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ) as decision:
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
    assert isinstance(decision.value.__cause__, history.TargetSeriesError)
    assert str(decision.value.__cause__) == (
        "required history mode is not ready: LIVE_PLUS_BACKTEST"
    )


@pytest.mark.parametrize("failure", ["git-missing", "commit-unresolved"])
def test_unprovable_committed_tool_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """An unprovable recorded tool identity is never repairable staleness.

    When the recorded commit's tool bytes cannot be resolved — git is
    unavailable, or the recorded SHA or tool path is absent from a shallow
    or fragment checkout — the verifier cannot prove the recorded identity
    was ever committed, so the generation fails closed as an integrity
    violation instead of licensing a rebuild.
    """

    import portfolio_target_series as history

    _install_gated_synthetic_history_parse(monkeypatch, history)
    fixture, generation = _retained_layered_history(tmp_path)

    artifacts = {
        name: (generation / name).read_bytes()
        for name in history.LAYERED_HISTORY_OUTPUT_NAMES
    }
    moved = json.loads((generation / "generation.json").read_bytes())
    moved["repository_sha"] = {"git-missing": "d" * 40, "commit-unresolved": "e" * 40}[
        failure
    ]
    moved_generation = history_tests.write_layered_history_generation(
        tmp_path / f"moved-{failure}",
        artifacts,
        history_tests._reidentify_manifest(moved),
    )
    monkeypatch.setattr(
        history,
        "_trusted_layered_history_code_identity",
        lambda: ("0" * 40, "f" * 64),
    )

    if failure == "git-missing":

        def unavailable_run(*args: object, **kwargs: object) -> Any:
            raise FileNotFoundError("git")

    else:

        class _Unresolved:
            returncode = 1
            stdout = b""

        def unavailable_run(*args: object, **kwargs: object) -> Any:
            return _Unresolved()

    monkeypatch.setattr(history.subprocess, "run", unavailable_run)

    with pytest.raises(
        history.TargetSeriesError,
        match="layered history committed tool identity is unavailable",
    ) as raised:
        history.verify_layered_history_generation(
            moved_generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "integrity"
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            moved_generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )


class _CommittedToolRevisions(NamedTuple):
    """Two real committed history-tool revisions and the module executing the later."""

    module: Any
    ancestor_sha: str
    ancestor_tool_sha256: str
    head_sha: str
    head_tool_sha256: str


def _commit_history_tool_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _CommittedToolRevisions:
    """Commit two real revisions of the history tool in a throwaway repository.

    The tool resolves a recorded commit against the repository its own file
    lives in, so proving a non-HEAD recorded commit end to end needs a real
    repository whose history holds this tool at two different revisions. The
    throwaway repository below commits an earlier tool revision and then the
    executing tool's own bytes on top of it, so HEAD differs from the
    recorded ancestor. A byte-identical copy of the executing tool is
    imported from that repository's ``tools`` directory and installed as the
    normally imported ``portfolio_target_series`` module, and the repository
    becomes the attended-preparation root. Nothing in the committed-tool
    resolution is replaced: the trusted-identity read, the recorded-commit
    ``git show``, the hash comparison, the classification, and the HEAD
    comparison all run for real against real commits.
    """

    import portfolio_target_series as executing_history

    for key in tuple(os.environ):
        if key.upper().startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)

    root = (tmp_path / "tool-revision-repository").resolve()
    tool_path = root / Path(HISTORY_TOOL_RELATIVE_PATH)
    tool_path.parent.mkdir(parents=True)

    def git(*arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        return completed.stdout

    git("init", "--quiet")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Tool Revision Test")
    git("config", "commit.gpgsign", "false")
    git("config", "core.autocrlf", "false")

    executing_bytes = Path(executing_history.__file__).read_bytes()
    revisions: list[tuple[str, str]] = []
    for content in (
        b'"""Earlier committed revision of the layered-history tool."""\n',
        executing_bytes,
    ):
        tool_path.write_bytes(content)
        git("add", HISTORY_TOOL_RELATIVE_PATH)
        git("commit", "--quiet", "--no-verify", "-m", "history tool revision")
        commit_sha = git("rev-parse", "HEAD").decode("ascii").strip()
        committed = git(
            "cat-file", "blob", f"{commit_sha}:{HISTORY_TOOL_RELATIVE_PATH}"
        )
        assert committed == content, "the commit must hold exactly the bytes written"
        revisions.append((commit_sha, hashlib.sha256(committed).hexdigest()))
    (ancestor_sha, ancestor_tool_sha256), (head_sha, head_tool_sha256) = revisions
    assert ancestor_sha != head_sha
    assert ancestor_tool_sha256 != head_tool_sha256

    spec = importlib.util.spec_from_file_location("portfolio_target_series", tool_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load the committed history tool revision")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "portfolio_target_series", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(preparation, "ROOT", root)
    return _CommittedToolRevisions(
        module=module,
        ancestor_sha=ancestor_sha,
        ancestor_tool_sha256=ancestor_tool_sha256,
        head_sha=head_sha,
        head_tool_sha256=head_tool_sha256,
    )


def test_legitimate_tool_revision_rebuilds_through_real_git_against_a_recorded_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded ancestor that truly holds the recorded tool bytes is staleness.

    The retained manifest records a real earlier commit and the true SHA-256
    of the tool bytes that commit holds, while the executing tool is a later
    commit with different bytes -- exactly what editing the tool after the
    build produces. No tool-identity seam is pinned: the verifier resolves
    the recorded commit with a real
    ``git show <recorded-sha>:tools/portfolio_target_series.py`` against a
    real repository, the hash it reads matches the recorded identity, the
    verdict is repairable ``staleness``, and the rebuild decision returns
    True after the real HEAD comparison. The only installed seam is the
    documented synthetic contract parse of the fixture module that builds
    the retained generation; the module under test is not patched. The
    lookup spy delegates to the real ``subprocess.run`` and pins the
    looked-up commit to the recorded ancestor, never HEAD, so an always-HEAD
    or wrong-path resolution -- which fails closed as integrity and silently
    disables the legitimate-revision rebuild -- fails here.
    """

    revisions = _commit_history_tool_revisions(tmp_path, monkeypatch)
    history = revisions.module
    history_tests._install_synthetic_history_authority(monkeypatch)
    fixture, generation = _retained_layered_history(tmp_path)
    ancestor_generation = _generation_recording_code_identity(
        generation,
        tmp_path / "ancestor-history",
        repository_sha=revisions.ancestor_sha,
        tool_sha256=revisions.ancestor_tool_sha256,
    )
    lookups = _spy_on_git_show_lookups(monkeypatch)

    with pytest.raises(
        history.TargetSeriesError,
        match="layered history code identity is not trusted",
    ) as raised:
        history.verify_layered_history_generation(
            ancestor_generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "staleness"
    assert _looked_up_tool_revisions(lookups) == [
        f"{revisions.ancestor_sha}:{HISTORY_TOOL_RELATIVE_PATH}"
    ]
    # The executing tool is the repository's HEAD, a different commit whose
    # tool bytes hash differently from the recorded identity.
    assert history._trusted_layered_history_code_identity() == (
        revisions.head_sha,
        revisions.head_tool_sha256,
    )
    assert revisions.head_tool_sha256 != revisions.ancestor_tool_sha256
    assert (
        preparation._retained_history_requires_rebuild(
            ancestor_generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
        is True
    )


def test_forged_tool_identity_at_a_recorded_ancestor_fails_closed_through_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged tool hash under a real recorded ancestor is integrity, not staleness.

    Same real repository and recorded ancestor as the legitimate-revision
    test, but the recorded ``tool_sha256`` matches no bytes that commit
    holds. The real ``git show`` against the recorded ancestor proves the
    mismatch, so the rebuild decision fails closed as an integrity
    violation. The spy pins the only lookup to the recorded ancestor: HEAD is
    never substituted for it, and the executing tool tree is never probed
    for a rebuild that was never licensed.
    """

    revisions = _commit_history_tool_revisions(tmp_path, monkeypatch)
    history = revisions.module
    history_tests._install_synthetic_history_authority(monkeypatch)
    fixture, generation = _retained_layered_history(tmp_path)
    forged_generation = _generation_recording_code_identity(
        generation,
        tmp_path / "forged-ancestor-history",
        repository_sha=revisions.ancestor_sha,
        tool_sha256="0" * 64,
    )
    lookups = _spy_on_git_show_lookups(monkeypatch)

    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ) as decision:
        preparation._retained_history_requires_rebuild(
            forged_generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
    assert isinstance(decision.value.__cause__, history.TargetSeriesError)
    assert decision.value.__cause__.kind == "integrity"
    assert _looked_up_tool_revisions(lookups) == [
        f"{revisions.ancestor_sha}:{HISTORY_TOOL_RELATIVE_PATH}"
    ]


def _admitted_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Path, Any]:
    """Build one retained generation bound to a real current admission proof.

    The verifier's current-source-admission comparison, the receipt and
    contract identities on disk, and the build all stay real. The synthetic
    producer below stands in for the real admission replay only, because
    the real replay loads the admission contract through the real
    financial-MVP loader, which rejects every synthetic provider fixture;
    it is the same documented seam the layered-history fixture module uses
    for contract parsing.
    """

    import portfolio_target_series as history

    admitted_sources = (
        "owner-account-live-history",
        "matrixport-account-generation",
        "quantbase-account-generation",
        "portfolio123-model-generation",
    )

    def synthetic_admission_authority(**kwargs: object) -> dict[str, object] | None:
        receipt_path = cast(Path, kwargs["provider_admission_receipt_path"])
        if receipt_path is None:
            return None
        contract_path = cast(Path, kwargs["contract_path"])
        financial_input = kwargs["financial_input_contract_path"]
        receipt_bytes = receipt_path.read_bytes()
        contract_bytes = cast(Path, financial_input or contract_path).read_bytes()
        manifest_path = cast(Path, kwargs["provider_ledger_manifest_path"])
        manifest_bytes = manifest_path.read_bytes()
        _contract, _strategies, sources, _corrections = (
            history_tests._synthetic_history_contract(contract_path)
        )
        generations = {
            source_id: {
                "as_of": sources[source_id]["current_generation"]["as_of"],
                "generation_id": sources[source_id]["current_generation"][
                    "generation_id"
                ],
                "sha256": sources[source_id]["current_generation"]["sha256"],
            }
            for source_id in admitted_sources
        }
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
        accepted_as_of = cast("date", kwargs["accepted_as_of"]).isoformat()
        proof = {
            "admission_receipt_sha256": receipt_sha256,
            "admission_receipt_size_bytes": len(receipt_bytes),
            "admission_observation_cutoff": accepted_as_of,
            "financial_cutoff": accepted_as_of,
            "financial_input_contract_census_sha256": contract_sha256,
            "financial_input_contract_raw_sha256": contract_sha256,
            "financial_input_contract_semantic_sha256": contract_sha256,
            "ledger_catalog_sha256": hashlib.sha256(
                cast(Path, kwargs["provider_ledger_catalog_path"]).read_bytes()
            ).hexdigest(),
            "ledger_generation_id": json.loads(manifest_bytes)["generation_id"],
            "ledger_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "run_observation_sha256": receipt_sha256,
            "schema_version": 1,
            "source_generations": [
                {"source_id": source_id, **generations[source_id]}
                for source_id in sorted(generations)
            ],
        }
        return {"proof": proof, "source_generations": generations}

    _install_verifier_history_seams(monkeypatch)
    monkeypatch.setattr(
        history, "_current_source_admission_authority", synthetic_admission_authority
    )
    monkeypatch.setattr(
        history_tests.target_series,
        "_current_source_admission_authority",
        synthetic_admission_authority,
    )

    fixture = history_tests._history_fixture(tmp_path)
    # The synthetic ledger must not fabricate returns on the declared NAV-gap
    # dates: the real admission comparison replays the ledger targets and
    # requires the retained account-live rows to match them exactly.
    catalog = json.loads(fixture["provider_ledger_catalog_path"].read_bytes())
    catalog["strategy_returns"] = [
        row
        for row in catalog["strategy_returns"]
        if not (
            row["strategy_id"] == "robuxio-crypto"
            and row["date"] in {"2025-12-09", "2025-12-10"}
        )
    ]
    for row in catalog["strategy_evidence"]:
        if row["strategy_id"] == "robuxio-crypto":
            row["account"]["return_observations"] = 3
    history_tests._write_json(fixture["provider_ledger_catalog_path"], catalog)
    ledger_generation_id = history_tests._core_generation_manifest(
        fixture["provider_ledger_manifest_path"],
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [
            history_tests._artifact(
                fixture["provider_ledger_catalog_path"],
                "normalized/provider-ledger.json",
            )
        ],
    )
    history_tests._pointer(
        fixture["provider_ledger_pointer_path"],
        ledger_generation_id,
        fixture["provider_ledger_manifest_path"],
    )
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    history_tests._update_source_generation(
        contract,
        "owner-account-live-history",
        fixture["provider_ledger_manifest_path"],
        fixture["as_of_date"],
    )
    history_tests._update_correction_generations(
        contract,
        "owner-account-live-history",
        fixture["provider_ledger_manifest_path"],
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    history_tests._restore_retained_echo_correction(contract)
    history_tests._write_json(fixture["contract_path"], contract)
    history_tests._refresh_materialized_segment_identities(fixture)

    receipt = (
        fixture["provider_ledger_manifest_path"].parent
        / "admit-or-declare-receipt.json"
    )
    receipt.write_bytes(
        canonical_json_bytes(
            {
                "admission_gate_status": "PASS",
                "provider_generations": [],
                "run_observation": {"financial_cutoff": "2026-01-10"},
            }
        )
    )
    financial_input = tmp_path / "admitted-financial-input-contract.json"
    financial_input.write_bytes(fixture["contract_path"].read_bytes())
    authorities = cast(
        "preparation._HistoryAuthorities",
        {
            "financial_input_contract_path": financial_input,
            "portfolio123_version_evidence_path": None,
            "provider_admission_receipt_path": receipt,
            "provider_ledger_catalog_path": fixture["provider_ledger_catalog_path"],
            "provider_ledger_manifest_path": fixture["provider_ledger_manifest_path"],
        },
    )
    artifacts, manifest_content = history_tests.build_layered_history_generation(
        repository_sha=history._trusted_layered_history_code_identity()[0],
        **fixture,
        provider_admission_receipt_path=receipt,
        financial_input_contract_path=financial_input,
    )
    generation = history_tests.write_layered_history_generation(
        tmp_path / "retained-history", artifacts, manifest_content
    )
    assert (
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], **authorities
        )["verification"]
        == "VALID"
    )
    return fixture, generation, authorities


def test_stale_source_admission_proof_kind_rebuilds_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained proof bound to the previous ledger admission is staleness.

    One admission cycle later the recomputed proof differs from the proof
    the retained manifest recorded, so the retained generation is one ledger
    cycle behind and, on a clean tool tree, is rebuilt.
    """

    import portfolio_target_series as history

    fixture, generation, authorities = _admitted_retained_history(tmp_path, monkeypatch)
    receipt = (
        fixture["provider_ledger_manifest_path"].parent
        / "admit-or-declare-receipt.json"
    )

    # One admission cycle later, a new run observation is admitted.
    receipt.write_bytes(
        canonical_json_bytes(
            {
                "admission_gate_status": "PASS",
                "provider_generations": [],
                "run_observation": {"financial_cutoff": "2026-01-11"},
            }
        )
    )
    with pytest.raises(
        history.TargetSeriesError, match="current source admission proof differs"
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], **authorities
        )
    assert raised.value.kind == "staleness"
    _install_clean_history_tool_tree(monkeypatch)
    assert (
        preparation._retained_history_requires_rebuild(
            generation, fixture["contract_path"], authorities
        )
        is True
    )


def test_stale_and_corrupt_retained_generation_fails_closed_before_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generation that is both stale and corrupted fails on the corruption.

    The retained generation below is one admission cycle behind — its
    recorded current-source-admission proof binds the previous ledger
    admission — and one of its artifacts carries a flipped byte. The
    verifier's integrity pre-pass must fail the corrupted artifact before
    the staleness classification can raise, so the rebuild decision
    hard-fails instead of rebuilding over corrupt evidence.
    """

    import portfolio_target_series as history

    fixture, generation, authorities = _admitted_retained_history(tmp_path, monkeypatch)
    receipt = (
        fixture["provider_ledger_manifest_path"].parent
        / "admit-or-declare-receipt.json"
    )

    # One admission cycle later, a new run observation is admitted, and a
    # byte inside one retained artifact is flipped.
    receipt.write_bytes(
        canonical_json_bytes(
            {
                "admission_gate_status": "PASS",
                "provider_generations": [],
                "run_observation": {"financial_cutoff": "2026-01-11"},
            }
        )
    )
    corrupted = bytearray((generation / "returns.csv").read_bytes())
    corrupted[len(corrupted) // 2] ^= 1
    (generation / "returns.csv").write_bytes(bytes(corrupted))

    with pytest.raises(
        history.TargetSeriesError, match="layered history output differs: returns.csv"
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], **authorities
        )
    assert raised.value.kind == "integrity"
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            generation, fixture["contract_path"], authorities
        )


def test_stale_account_live_ledger_kind_rebuilds_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account-live rows left on the previous ledger basis are staleness.

    The retained generation below holds the native (unconverted) Robuxio
    account-live rows while its manifest-bound conversion proof replays the
    USD basis, so it is one conversion-basis cycle behind and is rebuilt
    rather than failed. The Quantbase twin of this comparison cannot fire
    from the verifier (it is a builder-side check), so its raise site is
    asserted directly.
    """

    import portfolio_target_series as history

    _install_verifier_history_seams(monkeypatch)
    fixture = history_tests._matrixport_fx_fixture(tmp_path)
    artifacts, manifest_content = history_tests._build(fixture)
    generation = history_tests.write_layered_history_generation(
        tmp_path / "retained-history", artifacts, manifest_content
    )
    assert (
        history.verify_layered_history_generation(generation, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )

    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    with localcontext() as context:
        context.prec = 28
        native_returns = {
            "2025-12-07": format((Decimal("106") / Decimal("105") - 1), "f"),
            "2025-12-08": format((Decimal("107") / Decimal("106") - 1), "f"),
            "2025-12-11": format((Decimal("110") / Decimal("109") - 1), "f"),
        }
    for row in segments:
        if row["segment_id"] == "robuxio-crypto:account-live":
            row["return"] = native_returns[row["date"]]

    fields = history.CORRELATION_FIELDS_V2
    live_only = [
        {field: row[field] for field in fields}
        for row in segments
        if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    live_only.sort(key=lambda row: (row["series_id"], row["date"]))
    hybrid = [
        {field: row[field] for field in fields}
        for row in segments
        if row["mode_admission"]
        in {"INCLUDE_SELECTED_LIVE", "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"}
    ]
    hybrid.sort(
        key=lambda row: (
            row["series_id"],
            row["date"],
            0 if row["evidence_layer"] == "BACKTEST" else 1,
        )
    )

    manifest = json.loads(manifest_content)
    coverage = json.loads(artifacts["coverage.json"])
    selected = [
        row for row in segments if row["segment_id"] == "robuxio-crypto:account-live"
    ]
    identity = history._row_identity(selected)
    for strategy in coverage["strategies"]:
        if strategy["strategy_id"] != "robuxio-crypto":
            continue
        for summary in strategy["segments"]:
            if summary["segment_id"] == "robuxio-crypto:account-live":
                summary["sha256"] = identity["sha256"]

    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    contract["history_corrections"]["materialized_segment_identities"] = (
        history._materialized_segment_identities(segments, sources)
    )
    history_tests._write_json(fixture["contract_path"], contract)
    contract_bytes = fixture["contract_path"].read_bytes()
    manifest["product"]["contract_sha256"] = hashlib.sha256(
        canonical_json_bytes(json.loads(contract_bytes))
    ).hexdigest()
    for row in manifest["inputs"]:
        if row["input_id"] == "financial-mvp-contract":
            row["sha256"] = hashlib.sha256(contract_bytes).hexdigest()
            row["size_bytes"] = len(contract_bytes)

    history_tests._replace_output(
        artifacts, manifest, "segments.csv", history._segment_view_bytes(segments)
    )
    history_tests._replace_output(
        artifacts, manifest, "coverage.json", canonical_json_bytes(coverage)
    )
    history_tests._replace_output(
        artifacts, manifest, "live-only.csv", history._correlation_view_bytes(live_only)
    )
    history_tests._replace_output(
        artifacts,
        manifest,
        "live-plus-backtest.csv",
        history._correlation_view_bytes(hybrid),
    )
    history_tests._replace_output(
        artifacts, manifest, "returns.csv", history._correlation_view_bytes(hybrid)
    )
    stale_manifest_content = history_tests._reidentify_manifest(manifest)
    stale_generation = history_tests.write_layered_history_generation(
        tmp_path / "stale-history", artifacts, stale_manifest_content
    )

    with pytest.raises(
        history.TargetSeriesError,
        match="Matrixport account-live return differs from provider ledger",
    ) as raised:
        history.verify_layered_history_generation(
            stale_generation, fixture["contract_path"]
        )
    assert raised.value.kind == "staleness"
    _install_clean_history_tool_tree(monkeypatch)
    assert (
        preparation._retained_history_requires_rebuild(
            stale_generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
        is True
    )

    with pytest.raises(
        history.TargetSeriesError,
        match="Quantbase account-live return differs from provider ledger",
    ) as quantbase:
        history._quantbase_account_live_from_ledger(
            [{"date": "2024-10-16", "return": "0.01"}],
            [{"date": "2024-10-16", "return": "0.02"}],
            (),
        )
    assert quantbase.value.kind == "staleness"


def test_staleness_rebuild_refuses_uncommitted_history_tool_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirty history tool hard-fails the staleness rebuild; a clean one proceeds.

    The rebuild executes the history tool's on-disk bytes while recording the
    clean HEAD SHA, so before a staleness verdict licenses a rebuild the
    executing tool's bytes must match HEAD's committed bytes: uncommitted
    tool bytes fail closed — the operator commits or restores — while the
    same staleness on a clean tree still reaches the rebuild.
    """

    import portfolio_target_series as history

    _install_verifier_history_seams(monkeypatch)
    fixture, generation = _retained_layered_history(tmp_path)
    assert (
        history.verify_layered_history_generation(generation, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )

    monkeypatch.setattr(
        history,
        "_trusted_layered_history_code_identity",
        lambda: ("0" * 40, "f" * 64),
    )
    monkeypatch.setattr(
        history,
        "_committed_layered_history_tool_sha256",
        lambda _repository_sha: hashlib.sha256(
            Path(history.__file__).read_bytes()
        ).hexdigest(),
    )
    with pytest.raises(
        history.TargetSeriesError,
        match="layered history code identity is not trusted",
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "staleness"

    def committed_history_tool_bytes(relative_path: str) -> bytes:
        return (
            preparation.ROOT.joinpath(*relative_path.split("/")).read_bytes()
            + b"\n# uncommitted edit\n"
        )

    monkeypatch.setattr(
        preparation, "_head_committed_file_bytes", committed_history_tool_bytes
    )
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="layered history tool has uncommitted changes; refusing rebuild",
    ):
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )

    _install_clean_history_tool_tree(monkeypatch)
    assert (
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )
        is True
    )


def test_corrupt_retained_generation_fails_closed_from_real_tampered_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tampered artifact bytes hard-fail the retained history; no silent rebuild.

    One byte flipped inside a real retained artifact is corruption of the
    retained evidence, not a code or ledger cycle: the rebuild decision must
    raise instead of rebuilding over the corruption.
    """

    import portfolio_target_series as history

    _install_verifier_history_seams(monkeypatch)
    fixture, generation = _retained_layered_history(tmp_path)
    assert (
        history.verify_layered_history_generation(generation, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )

    corrupted = bytearray((generation / "returns.csv").read_bytes())
    corrupted[len(corrupted) // 2] ^= 1
    (generation / "returns.csv").write_bytes(bytes(corrupted))

    with pytest.raises(
        history.TargetSeriesError, match="layered history output differs: returns.csv"
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "integrity"
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )


def test_retained_inventory_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A differing generation inventory is corruption and never rebuilds."""

    import portfolio_target_series as history

    _install_verifier_history_seams(monkeypatch)
    fixture, generation = _retained_layered_history(tmp_path)
    (generation / "coverage.json").unlink()

    with pytest.raises(
        history.TargetSeriesError,
        match="layered history generation inventory differs",
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert raised.value.kind == "integrity"
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
        )


@pytest.mark.parametrize("primitive", ["iterdir", "read_bytes"])
def test_unreadable_retained_generation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primitive: str,
) -> None:
    """Unreadable retained evidence is wrapped or raw, and never rebuilds.

    An unreadable generation directory is wrapped by the verifier into an
    integrity TargetSeriesError; an unreadable artifact file escapes as a raw
    OSError, which the decision still fails closed on. Both OS failures are
    injected at the filesystem primitive scoped to the retained generation,
    because a real unreadable directory cannot be produced portably (Windows
    directory ACLs do not map to POSIX mode bits); every wrap, classification,
    and decision below them is real code.
    """

    import portfolio_target_series as history

    _install_verifier_history_seams(monkeypatch)
    fixture, generation = _retained_layered_history(tmp_path)

    if primitive == "iterdir":
        real_iterdir = Path.iterdir

        def unreadable(self: Path) -> Any:
            if self == generation:
                raise PermissionError("generation inventory is unreadable")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", unreadable)
        expected = "layered history generation inventory is unreadable"
    else:
        real_read_bytes = Path.read_bytes

        def unreadable(self: Path) -> Any:
            if self == generation / "coverage.json":
                raise PermissionError("coverage bytes are unreadable")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
        expected = "retained layered history is invalid"

    with pytest.raises(
        (history.TargetSeriesError, OSError),
    ) as raised:
        history.verify_layered_history_generation(
            generation, fixture["contract_path"], require_mode="LIVE_ONLY"
        )
    assert not isinstance(raised.value, history.TargetSeriesError) or (
        str(raised.value) == expected and raised.value.kind == "integrity"
    )
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="retained layered history is invalid",
    ):
        preparation._retained_history_requires_rebuild(
            generation,
            fixture["contract_path"],
            cast("preparation._HistoryAuthorities", {}),
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


FX_TEST_CUTOFF = "2026-09-17"
# The synthetic contract FX end and pin used by the FXB-1 worlds below. The FX
# tests rewrite the fixture's contract copy to carry them, so nothing here
# restates the real contract's admitted FX end.
FX_TEST_END = "2026-09-13"
FX_TEST_GENERATION = "f" * 64


def _write_ledger_generation(
    output: Path,
    catalog: dict[str, object],
    *,
    verification: VerificationStatus = VerificationStatus.VERIFIED,
    artifact_id: str = "normalized/provider-ledger.json",
) -> str:
    """Stage one minimal provider-ledger generation whose manifest binds its catalog.

    Preparation reads the staged catalog only after checking it against the
    generation manifest, so the fixture publishes a real, content-addressed
    manifest instead of the empty placeholder it used before FXB-1.
    """

    content = canonical_json_bytes(catalog)
    as_of = date(2026, 9, 17)
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(name="quant-engine-provider-ledger", version="5"),
        coverage=CoverageClaim.from_dict(
            {
                "boundary": "synthetic provider-ledger fixture",
                "fact_types": ["nav_observations"],
                "known_gaps": [],
                "observed_end": as_of.isoformat(),
                "observed_start": as_of.isoformat(),
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            }
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=as_of,
                schema_name="provider-ledger-catalog",
                schema_version="2",
            ),
        ),
        verification=verification,
    )
    generation = output / "generations" / manifest.generation_id
    generation.mkdir(parents=True, exist_ok=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "provider-ledger.json").write_bytes(content)
    (generation / "admit-or-declare-receipt.json").write_text("{}", encoding="utf-8")
    return manifest.generation_id


def _ledger_catalog(
    findings: list[dict[str, object]] | None = None,
    nav_observations: list[dict[str, object]] | None = None,
    *,
    fx_generation_id: str = FX_TEST_GENERATION,
) -> dict[str, object]:
    """A synthetic provider-ledger catalog carrying only what FXB-1 reads."""

    return {
        "as_of_date": FX_TEST_CUTOFF,
        "catalog_schema_version": 2,
        "nav_observations": nav_observations or [],
        "portfolio_fx_generation": {
            "generation_id": fx_generation_id,
            "selection": "OWNER_SELECTED_SYNTHETIC_FX",
        },
        "quality_findings": findings or [],
    }


def _iso_days(first: str, last: str) -> list[str]:
    start = date.fromisoformat(first)
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((date.fromisoformat(last) - start).days + 1)
    ]


def _hwm_period_rows(
    strategy_id: str, first: str, last: str
) -> list[dict[str, object]]:
    days = _iso_days(first, last)
    return [
        {"strategy_id": strategy_id, "interval_start": start, "interval_end": end}
        for start, end in zip(days, days[1:])
    ]


def _fx_finding(day: str, *, boundary: str = FX_TEST_END) -> dict[str, object]:
    """The finding the provider ledger emits for one unconverted native NAV day."""

    return {
        "fact_type": "nav_observations",
        "fx_boundary": boundary,
        "original_record_id": f"p-fx:{day}",
        "original_source_date": day,
        "provider_id": "p-fx",
        "record_id": f"p-fx:p-fx:{day}",
        "source_artifact_id": "csv/daily_holdings.csv",
        "source_record_id": f"p-fx:{day}",
        "source_row_sha256": "b" * 64,
        "status": "OUTSIDE_ADMITTED_FX_BOUNDARY",
    }


def _nav_row(provider: str, strategy: str, day: str, account: str) -> dict[str, object]:
    return {
        "account_id": account,
        "as_of_date": day,
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": "c" * 64,
        "mapping_status": "MAPPED",
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
        "provider_id": provider,
        "record_id": f"{provider}:nav-{day}",
        "source_record_id": f"nav-{day}",
        "strategy_id": strategy,
    }


class _FxWorld(NamedTuple):
    catalog: dict[str, object]
    memberships: list[dict[str, object]]
    periods: list[dict[str, object]]


def _fx_world(
    *, findings: bool = True, fx_strategy_ends: str = FX_TEST_END
) -> _FxWorld:
    """Two governed members: ``s-fx`` (USD basis ends early) and ``s-other``.

    With ``findings`` the ledger records every native NAV day of ``p-fx`` after the
    admitted FX end as an OUTSIDE_ADMITTED_FX_BOUNDARY finding. Without them the same
    early end is an unexplained shortfall that FXB-1 must not bound.
    """

    first_unconverted = (
        date.fromisoformat(FX_TEST_END) + timedelta(days=1)
    ).isoformat()
    catalog = _ledger_catalog(
        findings=(
            [_fx_finding(day) for day in _iso_days(first_unconverted, FX_TEST_CUTOFF)]
            if findings
            else []
        ),
        nav_observations=[
            _nav_row("p-fx", "s-fx", day, "account-fx-0001")
            for day in _iso_days("2026-09-10", FX_TEST_END)
        ]
        + [
            _nav_row("p-other", "s-other", day, "account-other-0001")
            for day in _iso_days("2026-09-10", FX_TEST_CUTOFF)
        ],
    )
    memberships = [
        {
            "strategy_id": "s-fx",
            "effective_start": "2026-09-10",
            "membership_evidence_sha256": "1" * 64,
        },
        {
            "strategy_id": "s-other",
            "effective_start": "2026-09-10",
            "membership_evidence_sha256": "2" * 64,
        },
    ]
    periods = _hwm_period_rows("s-fx", "2026-09-10", fx_strategy_ends) + (
        _hwm_period_rows("s-other", "2026-09-10", FX_TEST_CUTOFF)
    )
    return _FxWorld(catalog, memberships, periods)


def _bind_contract_fx_end(contract_path: Path, fx_end: str = FX_TEST_END) -> None:
    """Rewrite the fixture's contract copy so its one FX segment ends at ``fx_end``."""

    contract = json.loads(contract_path.read_bytes())
    (segment,) = [
        item
        for item in contract["source_segments"]
        if item["source_id"] == "portfolio-fx-rates"
    ]
    segment["interval"]["observed_end"] = fx_end
    segment["current_generation"] = {
        **segment["current_generation"],
        "generation_id": FX_TEST_GENERATION,
        "as_of": fx_end,
    }
    contract_path.write_bytes(json.dumps(contract, indent=2).encode() + b"\n")


def _capture_hwm_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[dict[str, str]], dict[str, object]]]:
    """Replace the governed HWM executor with a recorder of its grid and arguments."""

    from quant_engine.portfolio_ledger import hwm_governed_execution as hwm

    captured: list[tuple[list[dict[str, str]], dict[str, object]]] = []

    def compute(
        intervals: list[dict[str, str]], *_args: object, **kwargs: object
    ) -> dict[str, object]:
        captured.append((list(intervals), dict(kwargs)))
        return {}

    monkeypatch.setattr(hwm, "compute_governed_as_held_composite_hwm", compute)
    return captured


def _stage_attended_baseline(root: Path) -> tuple[str, str]:
    as_of = date.fromisoformat("2026-09-17")
    catalog_bytes = canonical_json_bytes(
        {
            "as_of_date": "2026-09-17",
            "baseline_schema_version": 1,
            "fixture": "attended baseline",
        }
    )
    catalog_sha = hashlib.sha256(catalog_bytes).hexdigest()
    report_bytes = b'{"status":"PASS"}\n'
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-attended-baseline", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic attended baseline fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/portfolio-baseline.json",
                sha256=catalog_sha,
                size_bytes=len(catalog_bytes),
                as_of_date=as_of,
                schema_name="portfolio-baseline-catalog",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                size_bytes=len(report_bytes),
                as_of_date=as_of,
                schema_name="independent-verification",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        root,
        manifest,
        {
            "normalized/portfolio-baseline.json": catalog_bytes,
            "verification/independent-verification.json": report_bytes,
        },
    )
    generation = root / "generations" / manifest.generation_id
    (generation / "portfolio-baseline.json").write_bytes(catalog_bytes)
    return manifest.generation_id, catalog_sha


def _stage_attended_attribution(root: Path, baseline: dict[str, str]) -> str:
    as_of = date.fromisoformat("2026-09-17")
    catalog_bytes = canonical_json_bytes(
        {"baseline": baseline, "catalog_schema_version": 1}
    )
    catalog_sha = hashlib.sha256(catalog_bytes).hexdigest()
    report_bytes = b'{"status":"PASS"}\n'
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-attended-attribution", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic attended attribution fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/strategy-attribution.json",
                sha256=catalog_sha,
                size_bytes=len(catalog_bytes),
                as_of_date=as_of,
                schema_name="strategy-attribution",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                size_bytes=len(report_bytes),
                as_of_date=as_of,
                schema_name="independent-verification",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        root,
        manifest,
        {
            "normalized/strategy-attribution.json": catalog_bytes,
            "verification/independent-verification.json": report_bytes,
        },
    )
    generation = root / "generations" / manifest.generation_id
    (generation / "strategy-attribution.json").write_bytes(catalog_bytes)
    return manifest.generation_id


def _import_report_before_stubs() -> None:
    """Load the report producer under the real kernel before any test patches it."""

    import portfolio_report  # noqa: F401


def test_preparation_stubs_do_not_leak_into_first_report_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stubbed preparation world must not become portfolio_report's kernel."""

    from quant_engine.portfolio_ledger import hwm_governed_execution as hwm

    real_verify = hwm.verify_governed_hwm_result
    # Simulate a session in which nothing has imported the report producer yet;
    # monkeypatch restores the original entries (or removes the fresh ones).
    for name in ("portfolio_report", "portfolio_attribution"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    _preparation_function_fixture(tmp_path, monkeypatch)
    assert hwm.verify_governed_hwm_result is not real_verify

    import portfolio_report

    assert portfolio_report.verify_governed_hwm_result is real_verify


def _preparation_function_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    history_rebuild_required: bool = False,
    ledger_catalog: dict[str, object] | None = None,
    hwm_memberships: list[dict[str, object]] | None = None,
    hwm_component_periods: list[dict[str, object]] | None = None,
    attribution_baseline_mismatch: str | None = None,
) -> tuple[Path, dict[str, int], dict[str, object]]:
    """Keep checkpoint control flow real while replacing only numerical producers.

    The staged ledger generation is real (manifest plus catalog) and the governed
    memberships carry ``strategy_id``, because the governed HWM grid ends at the
    FXB-1 common value-basis date of those strategies.
    """

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

    # The retained-attribution preflight lazily imports portfolio_attribution, which
    # imports portfolio_report, and portfolio_report binds verify_governed_hwm_result
    # by name at import.  Importing it first time under the stubs below would cache
    # the stub in sys.modules for every later test in the session.
    _import_report_before_stubs()

    source_selection = tmp_path / "selection.json"
    source_selection.write_text('{"files":{}}', encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_bytes(
        (
            preparation.ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    calendar = tmp_path / "calendar.json"
    calendar.write_text(
        '{"authority_evidence_sha256":"calendar","hwm_policy_sha256":"policy"}',
        encoding="utf-8",
    )
    history_root = tmp_path / "history"
    history_root.mkdir()
    baseline_source = tmp_path / "baseline-source"
    baseline_source.mkdir()
    attribution_root = tmp_path / "attribution-source"
    baseline_id, baseline_catalog_sha = _stage_attended_baseline(
        tmp_path / "retained-baseline"
    )
    attributed_baseline = {
        "generation_id": baseline_id,
        "catalog_sha256": baseline_catalog_sha,
    }
    if attribution_baseline_mismatch == "generation_id":
        attributed_baseline["generation_id"] = "c" * 64
    elif attribution_baseline_mismatch == "catalog_sha256":
        attributed_baseline["catalog_sha256"] = "c" * 64
    elif attribution_baseline_mismatch is not None:
        raise AssertionError("unsupported attribution baseline mismatch")
    attribution_id = _stage_attended_attribution(attribution_root, attributed_baseline)
    attribution_source = attribution_root / "generations" / attribution_id
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
        "expected_staged_baseline": attributed_baseline,
        "source_selection": str(source_selection),
    }
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        (tmp_path / f"{mode}-public.json").write_bytes(
            preparation.canonical_json_bytes({"acquisition_run": {"run_id": "a" * 64}})
        )
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
                "portfolio_returns",
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
                "ledger_returns",
                "provider_ledger",
                "attribution_baseline_reader",
                "value_basis_bounds",
                "layered_history_repository_dependencies",
                "sleeve_facts_release",
                "sleeve_account_facts",
                "quantbase_fund_facts",
                "nested_tag_map",
                "sleeve_disclosures",
                "sleeve_inputs",
                "sleeve_ledger",
                "sleeve_nav",
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
        # The real `_preparation_input_identity` records the evaluation clock beside the
        # financial cutoff. A stub that omits it pushes `_preparation_run_identity` into its
        # whole-dict fallback, which then compares `stage_inputs` too and refuses a legitimate
        # resume -- a consumer suite lying about the producer it replaces.
        "evaluation_date": "2026-09-19",
        "schema_version": 1,
        "source_phase": plan["source_phase"],
        "source_selection": preparation._definition(source_selection),
    }
    calls = {
        "attribution": 0,
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
        preparation,
        "_preparation_input_identity",
        # The real function derives `evaluation_date` from its `evaluated_on` argument. A stub
        # that returns a constant cannot distinguish two evaluation dates, which is exactly the
        # reuse the identity exists to prevent.
        lambda **kwargs: {**identity, "evaluation_date": kwargs["evaluated_on"]},
    )
    monkeypatch.setattr(preparation, "_selected_history_authorities", lambda _files: {})

    def selected_dependence_handoff(**kwargs: object) -> dict[str, object]:
        """Populate the authority fields used by the real final-handoff seam."""

        return {
            "schema_version": 1,
            "history_generation_root": str(
                Path(str(kwargs["history_source"])).resolve()
            ),
            "allocation_generation_root": str(
                Path(str(kwargs["baseline_source"])).resolve()
            ),
            "provider_ledger_generation_root": str(
                Path(str(config["provider_ledger_generation_root"])).resolve()
            ),
            "financial_input_contract_path": str(contract.resolve()),
            "provider_admission_receipt_path": str(
                (tmp_path / "receipt.json").resolve()
            ),
            "portfolio123_version_evidence_path": str(
                (tmp_path / "p123.json").resolve()
            ),
        }

    monkeypatch.setattr(
        preparation, "_selected_dependence_handoff", selected_dependence_handoff
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
        return {"generation_id": generation.name}

    def build_ledger(_recipe: Path, output: Path) -> dict[str, str]:
        calls["ledger"] += 1
        catalog = _ledger_catalog() if ledger_catalog is None else ledger_catalog
        return {"generation_id": _write_ledger_generation(output, catalog)}

    def build_baseline(_recipe: Path, _ledger: Path, output: Path) -> dict[str, str]:
        calls["baseline"] += 1
        generation_id, _catalog_sha = _stage_attended_baseline(output)
        return {"generation_id": generation_id}

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
    memberships = (
        [
            {
                "strategy_id": "s-default",
                "effective_start": "2026-09-16",
                "membership_evidence_sha256": "1" * 64,
            }
        ]
        if hwm_memberships is None
        else hwm_memberships
    )
    monkeypatch.setattr(
        hwm_inputs,
        "build_selected_hwm_inputs",
        lambda **_kwargs: {
            "calendar_sha256": "calendar",
            "component_periods": list(hwm_component_periods or []),
            "memberships": list(memberships),
        },
    )

    def compute_hwm(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["hwm"] += 1
        return {}

    monkeypatch.setattr(hwm, "compute_governed_as_held_composite_hwm", compute_hwm)
    monkeypatch.setattr(
        hwm, "verify_governed_hwm_result", lambda *_args, **_kwargs: None
    )

    def build_capital_input(
        **_kwargs: object,
    ) -> tuple[dict[str, object], dict[str, str]]:
        # The real builder returns the generation AND this machine's locators for it.
        # A stub that returns only the generation is a consumer suite lying about its
        # producer, which AGENTS.md A1.3 says does not satisfy the seam.
        calls["capital"] += 1
        return {}, {}

    monkeypatch.setattr(
        preparation, "_capital_input_builder", lambda: build_capital_input
    )
    monkeypatch.setattr(capital, "build_governed_capital_result", lambda *_args: {})
    monkeypatch.setattr(capital, "verify_governed_capital_result", lambda *_args: None)
    monkeypatch.setattr(outcomes, "verify_selected_capital_ledger", lambda *_args: None)

    def build_attribution(_recipe: Path, output: Path) -> dict[str, str]:
        calls["attribution"] += 1
        output.mkdir(parents=True, exist_ok=True)
        (output / "bundle.json").write_text("{}", encoding="utf-8")
        return {"destination": str(output)}

    monkeypatch.setattr(release, "build_attribution", build_attribution)
    return tmp_path / "preparation-spec.json", calls, identity


@pytest.mark.parametrize("mismatch", ["generation_id", "catalog_sha256"])
def test_stale_retained_attribution_fails_before_epoch_on_fresh_and_resumed_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    """The selected attribution must bind each fresh or reused staged baseline."""
    spec, calls, _identity = _preparation_function_fixture(
        tmp_path,
        monkeypatch,
        attribution_baseline_mismatch=mismatch,
    )
    output = tmp_path / "prepared"
    for resume in (False, True):
        with pytest.raises(
            preparation.AttendedPreparationError,
            match="attribution baseline identity differs",
        ):
            preparation.prepare_attended_inputs(
                preparation_spec_path=spec,
                output_root=output,
                requested_as_of="2026-09-17",
                evaluated_on="2026-09-19",
                release_commit="a" * 40,
                resume=resume,
            )
        assert calls["baseline"] == 1
        assert calls["rebind"] == 0
        assert calls["hwm"] == 0
        assert calls["capital"] == 0


def test_final_handoff_binds_allocation_to_selected_retained_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final handoff must use dependence's retained allocation authority."""

    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )

    selected = preparation._read(
        output / "preparation" / "dependence-selected-inputs.json",
        "selected dependence inputs",
    )
    source_run = preparation._read(
        output / "preparation" / "source-run.json", "source run"
    )
    attended = source_run["attended"]
    assert isinstance(attended, dict)
    allocation = attended["allocation_generation"]
    assert isinstance(allocation, dict)
    assert allocation["path"] == selected["allocation_generation_root"]
    assert allocation["path"] == str((tmp_path / "baseline-source").resolve())
    assert allocation["path"] != str((output / "stages" / "baseline").resolve())


def test_preparation_resume_refuses_a_changed_evaluation_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoints computed under one evaluation date are not reusable under another."""

    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"

    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )

    # Same financial cutoff, different execution clock: the plan bundles take it as
    # `evaluation_date`, the dependence spec as its `as_of_date`, and the governed HWM result
    # records it and derives freshness from it, so no checkpoint may be reused.
    with pytest.raises(
        preparation.AttendedPreparationError, match="preparation input identity differs"
    ):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-22",
            release_commit="a" * 40,
            resume=True,
        )


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
            "memberships": [
                {
                    "strategy_id": "s-default",
                    "effective_start": "2026-09-16",
                    "membership_evidence_sha256": "1" * 64,
                }
            ],
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


def _prepare_fx_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: _FxWorld,
) -> tuple[Path, dict[str, int]]:
    """Run the real preparation over ``world`` with the contract bound to its FX end."""

    spec, calls, _identity = _preparation_function_fixture(
        tmp_path,
        monkeypatch,
        ledger_catalog=world.catalog,
        hwm_memberships=world.memberships,
        hwm_component_periods=world.periods,
    )
    _bind_contract_fx_end(tmp_path / "contract.json")
    return spec, calls


def _prepare(spec: Path, output: Path, *, resume: bool = False) -> dict[str, Any]:
    return preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=resume,
    )


def test_h1a_the_governed_hwm_grid_ends_at_the_common_value_basis_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: a strategy whose USD basis ends at the admitted FX end ends the grid there.

    The ledger records the unconverted native days of ``s-fx`` after the contract's
    FX end as OUTSIDE_ADMITTED_FX_BOUNDARY findings. The composite needs every
    member, so the governed HWM runs to the common complete date, not the cutoff.
    """

    spec, _calls = _prepare_fx_world(tmp_path, monkeypatch, _fx_world())
    captured = _capture_hwm_compute(monkeypatch)
    output = tmp_path / "prepared"

    _prepare(spec, output)

    ((grid, kwargs),) = captured
    assert [(row["interval_start"], row["interval_end"]) for row in grid] == [
        ("2026-09-10", "2026-09-11"),
        ("2026-09-11", "2026-09-12"),
        ("2026-09-12", "2026-09-13"),
    ]
    assert kwargs["complete_through"] == FX_TEST_END
    checkpoint = preparation._read(
        output / "preparation" / "checkpoints" / "hwm.json", "HWM checkpoint"
    )
    assert checkpoint["stage"] == "hwm"


def test_h1b_without_findings_the_governed_hwm_grid_ends_at_the_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No FX finding, no bound: every member runs to the cutoff as before FXB-1."""

    world = _fx_world(findings=False, fx_strategy_ends=FX_TEST_CUTOFF)
    spec, _calls = _prepare_fx_world(tmp_path, monkeypatch, world)
    captured = _capture_hwm_compute(monkeypatch)

    _prepare(spec, tmp_path / "prepared")

    ((grid, kwargs),) = captured
    assert grid[0]["interval_start"] == "2026-09-10"
    assert grid[-1]["interval_end"] == FX_TEST_CUTOFF
    assert len(grid) == 7
    assert kwargs["complete_through"] == FX_TEST_CUTOFF


def test_h1d_an_unexplained_early_end_is_not_turned_into_a_common_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shortfall without an FX finding keeps the full grid, so the executor fails.

    ``s-fx`` stops at 2026-09-13 here with no OUTSIDE_ADMITTED_FX_BOUNDARY finding.
    Preparation must not derive a common date from the data (the sweep bypass): it
    hands the executor the full grid, whose coverage check then raises
    (tests/portfolio_ledger/test_hwm_governed_execution.py pins that half).
    """

    world = _fx_world(findings=False)
    spec, _calls = _prepare_fx_world(tmp_path, monkeypatch, world)
    captured = _capture_hwm_compute(monkeypatch)

    _prepare(spec, tmp_path / "prepared")

    ((grid, kwargs),) = captured
    assert grid[-1]["interval_end"] == FX_TEST_CUTOFF
    assert kwargs["complete_through"] == FX_TEST_CUTOFF


def test_h1e_a_gap_before_the_fx_end_does_not_move_the_grid_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: the grid ends at the admitted FX end, not at the member's last NAV day (W18).

    ``s-fx`` is FX-bounded (findings after the contract FX end) but its own NAV rows
    and governed periods stop two days before that end, a genuine gap. The grid and
    ``complete_through`` stay at the FX end, so the real executor rejects the uncovered
    member-period (tests/portfolio_ledger/test_hwm_governed_execution.py pins that
    half) instead of the grid ending early at the gap and hiding it. Every other world
    in this module ends ``s-fx`` exactly at the FX end, which is why only this one tells
    the contract end from a data-derived end.
    """

    gap_end = (date.fromisoformat(FX_TEST_END) - timedelta(days=2)).isoformat()
    world = _fx_world(fx_strategy_ends=gap_end)
    world.catalog["nav_observations"] = [
        row
        for row in world.catalog["nav_observations"]  # type: ignore[attr-defined]
        if row["provider_id"] != "p-fx" or row["as_of_date"] <= gap_end
    ]
    assert gap_end < FX_TEST_END
    spec, _calls = _prepare_fx_world(tmp_path, monkeypatch, world)
    captured = _capture_hwm_compute(monkeypatch)

    _prepare(spec, tmp_path / "prepared")

    ((grid, kwargs),) = captured
    assert [(row["interval_start"], row["interval_end"]) for row in grid] == [
        ("2026-09-10", "2026-09-11"),
        ("2026-09-11", "2026-09-12"),
        ("2026-09-12", "2026-09-13"),
    ]
    assert kwargs["complete_through"] == FX_TEST_END


def _break_boundary(world: _FxWorld) -> None:
    world.catalog["quality_findings"][0]["fx_boundary"] = "2026-09-12"  # type: ignore[index]


def _add_nav_after_the_fx_end(world: _FxWorld) -> None:
    world.catalog["nav_observations"].append(  # type: ignore[attr-defined]
        _nav_row("p-fx", "s-fx", "2026-09-14", "account-fx-0001")
    )


def _repin_ledger_fx_generation(world: _FxWorld) -> None:
    world.catalog["portfolio_fx_generation"]["generation_id"] = "e" * 64  # type: ignore[index]


def _drop_the_mapped_value_basis(world: _FxWorld) -> None:
    world.catalog["nav_observations"] = [
        row
        for row in world.catalog["nav_observations"]  # type: ignore[attr-defined]
        if row["provider_id"] != "p-fx"
    ]


def _add_a_period_after_the_value_basis_end(world: _FxWorld) -> None:
    world.periods.extend(_hwm_period_rows("s-fx", "2026-09-13", "2026-09-14"))


def _drop_a_membership_strategy_id(world: _FxWorld) -> None:
    del world.memberships[0]["strategy_id"]


@pytest.mark.parametrize(
    ("break_world", "message"),
    [
        (_break_boundary, "differs from the contract FX end"),
        (_add_nav_after_the_fx_end, "after the admitted FX end"),
        (_repin_ledger_fx_generation, "differs from the contract pin"),
        (_drop_the_mapped_value_basis, "no mapped value basis"),
        (_add_a_period_after_the_value_basis_end, "after the value-basis end"),
        (_drop_a_membership_strategy_id, "membership strategy id"),
    ],
)
def test_h1c_an_inconsistent_value_basis_stops_before_the_hwm_is_computed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    break_world: Any,
    message: str,
) -> None:
    """Every FXB-1 inconsistency fails closed in preparation, before any numbers."""

    world = _fx_world()
    break_world(world)
    spec, _calls = _prepare_fx_world(tmp_path, monkeypatch, world)
    captured = _capture_hwm_compute(monkeypatch)
    output = tmp_path / "prepared"

    with pytest.raises(preparation.AttendedPreparationError, match=message):
        _prepare(spec, output)

    assert captured == []
    assert not (output / "preparation" / "checkpoints" / "hwm.json").exists()


def _staged_ledger(tmp_path: Path, catalog: dict[str, object], **kwargs: Any) -> Path:
    generation_id = _write_ledger_generation(tmp_path / "ledger", catalog, **kwargs)
    return tmp_path / "ledger" / "generations" / generation_id


def _bound_contract(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "contract.json"
    path.write_bytes(
        (
            preparation.ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    _bind_contract_fx_end(path)
    return cast("dict[str, Any]", json.loads(path.read_bytes()))


def test_hwm_complete_through_is_the_common_value_basis_date_of_the_members(
    tmp_path: Path,
) -> None:
    world = _fx_world()
    generation = {"memberships": world.memberships, "component_periods": world.periods}
    kwargs = {
        "ledger_generation_root": _staged_ledger(tmp_path, world.catalog),
        "contract": _bound_contract(tmp_path),
        "cutoff": FX_TEST_CUTOFF,
    }

    common = preparation._hwm_complete_through(hwm_generation=generation, **kwargs)
    assert common == FX_TEST_END
    only_other = {
        "memberships": world.memberships[1:],
        "component_periods": world.periods,
    }
    unbounded = preparation._hwm_complete_through(hwm_generation=only_other, **kwargs)
    assert unbounded == FX_TEST_CUTOFF


def _tamper_the_catalog_bytes(generation: Path) -> Path:
    catalog = generation / "provider-ledger.json"
    catalog.write_bytes(catalog.read_bytes() + b" ")
    return generation


def _remove_the_catalog(generation: Path) -> Path:
    (generation / "provider-ledger.json").unlink()
    return generation


def _corrupt_the_manifest(generation: Path) -> Path:
    (generation / "generation.json").write_bytes(b"{}")
    return generation


def _rename_the_generation(generation: Path) -> Path:
    return generation.rename(generation.with_name("0" * 64))


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_tamper_the_catalog_bytes, "catalog differs from its manifest"),
        (_remove_the_catalog, "generation is unavailable"),
        (_corrupt_the_manifest, "manifest is invalid"),
        (_rename_the_generation, "differs from its manifest identity"),
    ],
)
def test_hwm_complete_through_reads_only_a_catalog_its_manifest_binds(
    tmp_path: Path, tamper: Any, message: str
) -> None:
    world = _fx_world()
    generation = tamper(_staged_ledger(tmp_path, world.catalog))

    with pytest.raises(preparation.AttendedPreparationError, match=message):
        preparation._hwm_complete_through(
            hwm_generation={
                "memberships": world.memberships,
                "component_periods": world.periods,
            },
            ledger_generation_root=generation,
            contract=_bound_contract(tmp_path),
            cutoff=FX_TEST_CUTOFF,
        )


@pytest.mark.parametrize(
    ("verification", "artifact_id", "message"),
    [
        (VerificationStatus.PENDING, "normalized/provider-ledger.json", "not verified"),
        (
            VerificationStatus.VERIFIED,
            "normalized/another-catalog.json",
            "not bound by its manifest",
        ),
    ],
)
def test_hwm_complete_through_needs_a_verified_manifest_that_declares_the_catalog(
    tmp_path: Path,
    verification: VerificationStatus,
    artifact_id: str,
    message: str,
) -> None:
    world = _fx_world()
    generation = _staged_ledger(
        tmp_path, world.catalog, verification=verification, artifact_id=artifact_id
    )

    with pytest.raises(preparation.AttendedPreparationError, match=message):
        preparation._hwm_complete_through(
            hwm_generation={
                "memberships": world.memberships,
                "component_periods": world.periods,
            },
            ledger_generation_root=generation,
            contract=_bound_contract(tmp_path),
            cutoff=FX_TEST_CUTOFF,
        )


def test_the_hwm_stage_code_identity_covers_the_value_basis_bound_module() -> None:
    """The module the stage executes must be part of the stage's checkpoint identity."""

    assert [
        stage
        for stage, names in preparation._PREPARATION_STAGE_CODE.items()
        if "value_basis_bounds" in names
    ] == ["hwm"]
    assert preparation._runtime_path("value_basis_bounds") == (
        preparation.ROOT
        / "packages"
        / "portfolio-ledger"
        / "src"
        / "quant_engine"
        / "portfolio_ledger"
        / "value_basis_bounds.py"
    )


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
            "code",
            "value_basis_bounds",
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


def test_attribution_reader_code_change_invalidates_epoch_attribution_and_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader code changes invalidate dependent checkpoints but reuse prior inputs."""
    assert [
        stage
        for stage, names in preparation._PREPARATION_STAGE_CODE.items()
        if "attribution_baseline_reader" in names
    ] == ["epoch", "attribution"]
    assert preparation._runtime_path("attribution_baseline_reader") == (
        preparation.ROOT / "tools" / "portfolio_attribution.py"
    )

    spec, calls, identity = _preparation_function_fixture(tmp_path, monkeypatch)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    attribution_checkpoint_path = preparation._checkpoint_path(output, "attribution")
    handoff_checkpoint_path = preparation._checkpoint_path(output, "final-handoff")
    old_attribution = preparation._read(
        attribution_checkpoint_path, "initial attribution checkpoint"
    )
    old_handoff = preparation._read(
        handoff_checkpoint_path, "initial final-handoff checkpoint"
    )
    initial_calls = dict(calls)

    code = identity["code"]
    assert isinstance(code, dict)
    code["attribution_baseline_reader"] = {"sha256": "changed"}
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
        resume=True,
    )

    new_attribution = preparation._read(
        attribution_checkpoint_path, "updated attribution checkpoint"
    )
    new_handoff = preparation._read(
        handoff_checkpoint_path, "updated final-handoff checkpoint"
    )
    assert new_attribution["input_identity"] != old_attribution["input_identity"]
    assert new_handoff["input_identity"] != old_handoff["input_identity"]
    assert calls["baseline"] == initial_calls["baseline"]
    assert calls["attribution"] == initial_calls["attribution"] + 1
    assert calls["rebind"] == initial_calls["rebind"] + 1
    assert calls["hwm"] == initial_calls["hwm"] + 1
    assert calls["capital"] == initial_calls["capital"] + 1
    reusable_upstream = {
        "history",
        "dependence",
        "dependence_verify",
        "plan",
        "ledger",
    }
    assert {name: calls[name] for name in calls if name in reusable_upstream} == {
        name: initial_calls[name] for name in initial_calls if name in reusable_upstream
    }


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
        # The runner marks the root it creates, so preparation receives that hook.
        assert callable(kwargs.pop("on_create"))
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
        actual: object, actual_root: Path, *, resume: bool, storage: object
    ) -> tuple[int, dict[str, object]]:
        events.append("execute")
        assert actual is preflight
        assert actual_root == output
        assert resume is False
        assert isinstance(storage, run_storage.AttendedStorage)
        return 0, {"status": "EXECUTED"}

    # Execution validates declaratively (pin matrix, source phase) before admitting.
    monkeypatch.setattr(
        preparation,
        "plan_attended_inputs",
        lambda **_kwargs: {"status": "PLANNED", "source_phase": {}},
    )
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
                "pid": os.getpid(),
                "started_at": "2026-09-19T12:00:00+00:00",
                "status": "RUNNING",
            }
        ]
        return {"financial_stage_inputs": {}, "source_run": source_run}

    def preflight(_args: object) -> dict[str, object]:
        events.append("preflight")
        return {"prepared": True}

    def execute(
        actual: object, _root: Path, *, resume: bool, storage: object
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
                "pid": os.getpid(),
                "started_at": "2026-09-19T12:00:00+00:00",
                "status": "PASSED",
            }
        ],
        "schema_version": 3,
    }
    assert events == ["prepare", "preflight", "execute"]
    # The runner created this root, so it carries the marker retire looks for.
    marker = json.loads((output / "attended-run-root.json").read_text("utf-8"))
    assert marker["kind"] == run_storage.RUN_MARKER_KIND
    assert marker["run_root"] == str(output)


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
            "pid": os.getpid(),
            "started_at": "2026-09-19T12:00:00+00:00",
            "status": "CANCELLED",
        },
        {
            "attempt": 2,
            "ended_at": "2026-09-19T12:01:01+00:00",
            "pid": os.getpid(),
            "started_at": "2026-09-19T12:01:00+00:00",
            "status": "PASSED",
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


def _with_sleeve_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, section: object
) -> None:
    original = preparation._validated_inputs

    def with_section(*args: object) -> tuple[dict[str, object], object]:
        config, plan = original(*args)
        return {**config, "sleeve_facts": section}, plan

    monkeypatch.setattr(preparation, "_validated_inputs", with_section)


def _sleeve_section(tmp_path: Path) -> dict[str, object]:
    manifest = tmp_path / "sleeve-policy-manifest.json"
    # the manifest pins the governed tag map; the preparation reads the parents' first
    # sleeve days from it for the static-snapshot receipts and the recipe
    tag_map = Path(__file__).resolve().parents[2] / (
        "ops/mvp2-release/inputs/sleeves/nested-strategy-tag-map.json"
    )
    (tmp_path / "nested-strategy-tag-map.json").write_bytes(tag_map.read_bytes())
    manifest.write_bytes(
        canonical_json_bytes(
            {
                "policy": "synthetic",
                "sleeve-account-facts": {
                    "tag_map": {"path": "nested-strategy-tag-map.json"}
                },
            }
        )
    )
    return {
        "policy_manifest": preparation._definition(manifest),
        "ibkr_generation_root": str(tmp_path / "ibkr-root"),
        "quantbase_source_dir": str(tmp_path / "quantbase-sources"),
    }


def test_a_sleeve_policy_manifest_without_the_tag_map_pin_is_refused(
    tmp_path: Path,
) -> None:
    section = _sleeve_section(tmp_path)
    manifest = tmp_path / "sleeve-policy-manifest.json"
    manifest.write_bytes(canonical_json_bytes({"policy": "synthetic"}))
    section["policy_manifest"] = preparation._definition(manifest)
    with pytest.raises(
        preparation.AttendedPreparationError, match="sleeve policy manifest differs"
    ):
        preparation._sleeve_tag_map_path(section)


def test_sleeve_facts_are_built_staged_replayed_and_checkpointed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured sleeve facts reach the epoch stage and the HWM replay, byte for byte."""

    import build_sleeve_facts_release as sleeve_release
    import portfolio_hwm_selected_inputs as hwm_inputs
    import stage_current_epoch_manifest as epoch_stage

    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    section = _sleeve_section(tmp_path)
    _with_sleeve_config(monkeypatch, tmp_path, section)
    built: dict[str, object] = {}

    def build(**kwargs: Any) -> dict[str, object]:
        built.update(kwargs)
        destination = Path(kwargs["output_dir"])
        files = []
        for name in ("sleeve-account-facts.json", "quantbase-fund-facts.json"):
            payload = canonical_json_bytes({"artifact": name})
            (destination / name).write_bytes(payload)
            files.append({"path": name})
        return {"files": files}

    monkeypatch.setattr(sleeve_release, "build_release_sleeve_facts", build)
    staged: dict[str, object] = {}
    previous_stage = epoch_stage.stage_current_epoch_manifest

    def stage(**kwargs: Any) -> dict[str, str]:
        staged["paths"] = kwargs["sleeve_facts_paths"]
        return previous_stage(**kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(epoch_stage, "stage_current_epoch_manifest", stage)
    replayed: list[object] = []
    for name in ("derive_selected_hwm_calendar_authority", "build_selected_hwm_inputs"):
        previous = getattr(hwm_inputs, name)

        def spy(*, _previous: Any = previous, **kwargs: Any) -> Any:
            replayed.append(kwargs["sleeve_facts_paths"])
            return _previous(**kwargs)

        monkeypatch.setattr(hwm_inputs, name, spy)

    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )

    destination = output / "preparation" / "sleeve-facts"
    expected = (
        destination / "sleeve-account-facts.json",
        destination / "quantbase-fund-facts.json",
    )
    assert staged["paths"] == expected
    assert replayed == [expected, expected]
    # the staged ledger generation is the one the build reads its rows from
    assert Path(str(built["provider_ledger_generation_dir"])).parent == (
        output / "stages" / "ledger" / "generations"
    )
    assert built["ibkr_generation_root"] == (tmp_path / "ibkr-root").resolve()
    assert built["quantbase_source_dir"] == (tmp_path / "quantbase-sources").resolve()
    checkpoint = preparation._read(
        output / "preparation" / "checkpoints" / "epoch.json", "epoch checkpoint"
    )
    assert checkpoint["result"]["sleeve_facts"] == [
        preparation._definition(path) for path in expected
    ]
    assert "sleeve_facts" in checkpoint["outputs"]
    assert (
        checkpoint["input_identity"]["inputs"]["sleeve_policy_manifest"]
        == (section["policy_manifest"])
    )


def test_no_sleeve_configuration_selects_no_nested_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import portfolio_hwm_selected_inputs as hwm_inputs
    import stage_current_epoch_manifest as epoch_stage

    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    staged: dict[str, object] = {}
    previous_stage = epoch_stage.stage_current_epoch_manifest

    def stage(**kwargs: Any) -> dict[str, str]:
        staged["paths"] = kwargs["sleeve_facts_paths"]
        return previous_stage(**kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(epoch_stage, "stage_current_epoch_manifest", stage)
    output = tmp_path / "prepared"
    preparation.prepare_attended_inputs(
        preparation_spec_path=spec,
        output_root=output,
        requested_as_of="2026-09-17",
        evaluated_on="2026-09-19",
        release_commit="a" * 40,
    )
    assert staged["paths"] == ()
    checkpoint = preparation._read(
        output / "preparation" / "checkpoints" / "epoch.json", "epoch checkpoint"
    )
    assert checkpoint["result"]["sleeve_facts"] == []
    assert "sleeve_policy_manifest" not in checkpoint["input_identity"]["inputs"]
    assert hwm_inputs is not None


def test_a_refused_sleeve_build_stops_before_the_epoch_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import build_sleeve_facts_release as sleeve_release

    spec, calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    _with_sleeve_config(monkeypatch, tmp_path, _sleeve_section(tmp_path))

    def refuse(**_kwargs: object) -> dict[str, object]:
        raise sleeve_release.SleeveFactsReleaseError("a pin differs")

    monkeypatch.setattr(sleeve_release, "build_release_sleeve_facts", refuse)
    output = tmp_path / "prepared"
    with pytest.raises(preparation.AttendedPreparationError, match="a pin differs"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=output,
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
        )
    assert calls["hwm"] == 0
    assert not (output / "preparation" / "checkpoints" / "epoch.json").exists()


@pytest.mark.parametrize(
    "section",
    [
        {"policy_manifest": {}},
        {"policy_manifest": {}, "ibkr_generation_root": "x"},
        "not an object",
    ],
)
def test_a_malformed_sleeve_configuration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: object
) -> None:
    spec, _calls, _identity = _preparation_function_fixture(tmp_path, monkeypatch)
    _with_sleeve_config(monkeypatch, tmp_path, section)
    with pytest.raises(preparation.AttendedPreparationError, match="configuration"):
        preparation.prepare_attended_inputs(
            preparation_spec_path=spec,
            output_root=tmp_path / "prepared",
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-19",
            release_commit="a" * 40,
        )


def _ledger_dir(tmp_path: Path) -> Path:
    """A ledger generation directory holding an empty catalog (no parent account in it)."""

    directory = tmp_path / "ledger-generation"
    directory.mkdir(exist_ok=True)
    (directory / "provider-ledger.json").write_text("{}", encoding="utf-8")
    return directory


def test_selected_sleeve_facts_without_a_ledger_generation_are_refused(
    tmp_path: Path,
) -> None:
    """The perimeter reconciliation needs the ledger: no silent release without it."""

    section = _sleeve_section(tmp_path)
    facts = tmp_path / "facts.json"
    facts.write_text(
        '{"artifact_id": "sleeve-account-facts", "provider_id": "ibkr"}',
        encoding="utf-8",
    )
    with pytest.raises(
        preparation.AttendedPreparationError, match="need the provider ledger"
    ):
        preparation._build_sleeve_disclosure_sources(
            section,
            facts_paths=(facts,),
            destination=tmp_path,
            ledger_generation_dir=None,
        )


def test_disclosure_sources_are_written_from_the_pinned_policy_inputs(
    tmp_path: Path,
) -> None:
    """The sources hash-bind the governed disclosure inputs the policy manifest pins."""

    import json

    section = _sleeve_section(tmp_path)
    inputs = {
        "schema_version": 1,
        "artifact_id": "sleeve-disclosure-inputs",
    }
    raw = json.dumps(inputs).encode()
    (tmp_path / "disclosure-inputs.json").write_bytes(raw)
    manifest = json.loads((tmp_path / "sleeve-policy-manifest.json").read_bytes())
    manifest["disclosure-inputs"] = {
        "path": "disclosure-inputs.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    (tmp_path / "sleeve-policy-manifest.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    section["policy_manifest"] = preparation._definition(
        tmp_path / "sleeve-policy-manifest.json"
    )
    facts = tmp_path / "facts.json"
    facts.write_bytes(
        json.dumps(
            {"artifact_id": "sleeve-account-facts", "provider_id": "ibkr"}
        ).encode()
    )
    destination = tmp_path / "out"
    destination.mkdir()
    path = preparation._build_sleeve_disclosure_sources(
        section,
        facts_paths=(facts,),
        destination=destination,
        ledger_generation_dir=_ledger_dir(tmp_path),
    )
    assert path == destination / "sleeve-disclosure-sources.json"
    written = json.loads(path.read_bytes())
    assert written["schema"] == "sleeve-disclosure-sources/1"
    assert written["predecessor"] is None
    # no sleeves selected: no sources; a pin that differs from the file is refused
    assert (
        preparation._build_sleeve_disclosure_sources(
            None,
            facts_paths=(facts,),
            destination=destination,
            ledger_generation_dir=_ledger_dir(tmp_path),
        )
        is None
    )
    (tmp_path / "disclosure-inputs.json").write_bytes(raw + b" ")
    with pytest.raises(
        preparation.AttendedPreparationError, match="differ from their pin"
    ):
        preparation._build_sleeve_disclosure_sources(
            section,
            facts_paths=(facts,),
            destination=tmp_path / "again",
            ledger_generation_dir=_ledger_dir(tmp_path),
        )


def test_disclosure_sources_read_the_pinned_declared_funding_events_or_refuse(
    tmp_path: Path,
) -> None:
    import json

    section = _sleeve_section(tmp_path)
    declared = {
        "funding_events": {"s2": {"2026-06-23": {"kind": "IN", "note": "plus margin"}}}
    }
    raw = json.dumps(declared).encode()
    (tmp_path / "declared.json").write_bytes(raw)
    manifest = json.loads((tmp_path / "sleeve-policy-manifest.json").read_bytes())
    manifest["sleeve-account-facts"] = {
        "declared_inputs": {
            "path": "declared.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    }
    (tmp_path / "sleeve-policy-manifest.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    section["policy_manifest"] = preparation._definition(
        tmp_path / "sleeve-policy-manifest.json"
    )
    facts = tmp_path / "facts.json"
    facts.write_bytes(
        json.dumps(
            {"artifact_id": "sleeve-account-facts", "provider_id": "ibkr"}
        ).encode()
    )
    out = tmp_path / "out"
    out.mkdir()
    path = preparation._build_sleeve_disclosure_sources(
        section,
        facts_paths=(facts,),
        destination=out,
        ledger_generation_dir=_ledger_dir(tmp_path),
    )
    assert path is not None
    written = json.loads(path.read_bytes())
    assert written["margin_funding_events"] == [
        {"sleeve_id": "s2", "day": "2026-06-23", "kind": "IN", "note": "plus margin"}
    ]
    (tmp_path / "declared.json").write_bytes(raw + b" ")
    with pytest.raises(
        preparation.AttendedPreparationError, match="declared inputs differ"
    ):
        preparation._build_sleeve_disclosure_sources(
            section,
            facts_paths=(facts,),
            destination=tmp_path / "again",
            ledger_generation_dir=_ledger_dir(tmp_path),
        )


class _AnyConfig(dict):  # type: ignore[type-arg]
    """A config whose every required key resolves to a nested placeholder."""

    def __missing__(self, key: str) -> "_AnyConfig":
        return _AnyConfig()


def test_ledger_stage_identity_binds_the_model_vs_actual_pin() -> None:
    base = _AnyConfig()
    without = preparation._preparation_stage_inputs(base, "c" * 40)["ledger"]
    assert "model_vs_actual_input" not in without
    pinned = _AnyConfig(model_vs_actual_input={"path": "a", "sha256": "1"})
    changed = _AnyConfig(model_vs_actual_input={"path": "a", "sha256": "2"})
    first = preparation._preparation_stage_inputs(pinned, "c" * 40)["ledger"]
    second = preparation._preparation_stage_inputs(changed, "c" * 40)["ledger"]
    assert first["model_vs_actual_input"] != second["model_vs_actual_input"]
    assert first != without


def test_the_model_vs_actual_pin_is_verified_by_the_specification_pin_check(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "audit-bridges.json"
    audit.write_bytes(b'{"synthetic": true}')
    good = {
        "path": str(audit),
        "sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
        "size_bytes": audit.stat().st_size,
    }
    spec = {"model_vs_actual_input": good}
    preparation._pins(spec, "preparation specification")
    assert preparation._pin(good, "model vs actual input") == audit.resolve()
    with pytest.raises(preparation.AttendedPreparationError, match="pin differs"):
        preparation._pins(
            {"model_vs_actual_input": {**good, "sha256": "0" * 64}},
            "preparation specification",
        )
    audit.unlink()
    with pytest.raises(preparation.AttendedPreparationError, match="unavailable"):
        preparation._pins(spec, "preparation specification")


def test_ledger_stage_identity_binds_the_weekly_gap_fill_pin() -> None:
    base = _AnyConfig()
    without = preparation._preparation_stage_inputs(base, "c" * 40)["ledger"]
    assert "weekly_gap_fill_spec" not in without
    pinned = _AnyConfig(weekly_gap_fill_spec={"path": "a", "sha256": "1"})
    changed = _AnyConfig(weekly_gap_fill_spec={"path": "a", "sha256": "2"})
    first = preparation._preparation_stage_inputs(pinned, "c" * 40)["ledger"]
    second = preparation._preparation_stage_inputs(changed, "c" * 40)["ledger"]
    assert first["weekly_gap_fill_spec"] != second["weekly_gap_fill_spec"]
