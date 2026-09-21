from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_qc_under_test", ROOT / "tools" / "portfolio_qc.py"
)
assert SPEC is not None and SPEC.loader is not None
qc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qc)


def _proof() -> dict[str, Any]:
    return {
        "artifact_sha256s": {"financial-outcome-hwm": "b" * 64},
        "capital_result_id": "c" * 64,
        "consumers": {"report": {"status": "PASS"}},
        "outcome_ids": list(qc.REQUIRED_OUTCOMES),
        "selected_cells": {"financial-outcome-decomposition": {"status": "AVAILABLE"}},
        "status": "PASS",
        "work_counters": {
            "correlation_build_calls": 0,
            "full_rederivation_count": 0,
            "grid_search_calls": 0,
            "model_fit_calls": 0,
            "receipt_reuse_count": 1,
        },
    }


def _spec(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "root"
    coordination = tmp_path / "coordination"
    root.mkdir(exist_ok=True)
    coordination.mkdir(exist_ok=True)
    source = tmp_path / "launcher.json"
    source.write_text('{"launcher":1}', encoding="utf-8")
    companion = SimpleNamespace(
        companion_id="launcher",
        relative_path="launcher.json",
        root_id="portfolio",
        sha256=qc._sha256(source),
        source_path=source,
    )
    return SimpleNamespace(
        code_commit="1" * 40,
        companions=(companion,),
        coordination_root=coordination,
        gates=(),
        release_generations={"portfolio": "2" * 64},
        roots={"portfolio": root},
        spec_sha256="a" * 64,
        state_root=tmp_path,
    )


def _write_preflight(evidence: Path, spec: Any) -> Path:
    state = qc._production_snapshot(spec)
    return qc._write_receipt(
        evidence,
        {
            "candidate_qa": {"financial_outcomes_status": "PASS"},
            "kind": "portfolio-release-preflight-v1",
            "plan": qc._expected_plan(spec),
            "production_state_after": state,
            "production_state_before": state,
            "spec_sha256": spec.spec_sha256,
            "status": "PASS",
        },
    )


def test_financial_evidence_separates_semantic_identity_from_work_counters() -> None:
    cold = _proof()
    cold["work_counters"]["full_rederivation_count"] = 1
    cold["work_counters"]["receipt_reuse_count"] = 0
    warm = json.loads(json.dumps(cold))
    warm["work_counters"] = {
        "correlation_build_calls": 0,
        "full_rederivation_count": 0,
        "grid_search_calls": 0,
        "model_fit_calls": 0,
        "receipt_reuse_count": 1,
    }

    cold_evidence = qc._financial_evidence(cold)
    warm_evidence = qc._financial_evidence(warm)

    assert (
        cold_evidence["financial_outcomes_sha256"]
        == warm_evidence["financial_outcomes_sha256"]
    )
    assert cold_evidence["work_counters"] != warm_evidence["work_counters"]


def test_ci_runs_complete_vertical_capital_attribution_and_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(qc, "_require_ci_targets", lambda: None)
    monkeypatch.setattr(
        qc, "_run_checked", lambda command, **_kwargs: commands.append(tuple(command))
    )

    assert qc.main(["--profile", "ci"]) == 0

    assert "tests/core/test_portfolio_mvp2_vertical.py" in commands[0]
    assert "tests/core/test_portfolio_mvp2_product.py" in commands[0]
    assert "tests/portfolio_analytics/test_portfolio_invested_capital.py" in commands[0]
    assert "tests/portfolio_attribution" in commands[0]
    assert "tests/portfolio_operations" in commands[0]
    assert commands[1][1].endswith("check_repository_boundary.py")


def test_ci_names_missing_complete_vertical_prerequisite(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(qc, "CI_TARGETS", ("tests/core/absent-vertical.py",))

    assert qc.main(["--profile", "ci"]) == 2
    assert (
        json.loads(capsys.readouterr().out)["prerequisite"]
        == "CI required target: tests/core/absent-vertical.py"
    )


def test_pre_pr_names_missing_exact_head_prerequisite(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert qc.main(["--profile", "pre-pr"]) == 2
    assert (
        json.loads(capsys.readouterr().out)["prerequisite"] == "expected exact head SHA"
    )


@pytest.mark.parametrize(
    "dirty,remote_head,reason",
    [
        (True, "a" * 40, "uncommitted"),
        (False, "b" * 40, "PR head"),
    ],
)
def test_pre_pr_cannot_certify_different_code(monkeypatch, dirty, remote_head, reason):
    def git(command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=("a" * 40 + "\n")
            if "rev-parse" in command
            else (" M changed.py\n" if dirty else ""),
        )

    monkeypatch.setattr(qc.subprocess, "run", git)
    monkeypatch.setattr(
        qc,
        "_run_checked",
        lambda *_args, **_kwargs: json.dumps(
            {
                "ready": True,
                "head": remote_head,
            }
        ),
    )
    args = SimpleNamespace(
        expected_head="a" * 40,
        pr=374,
        test_target=["test.py"],
        changed_path=["changed.py"],
    )
    with pytest.raises(qc.QualityControlFailure, match=reason):
        qc._pre_pr(args)


def test_workstation_uses_workspace_local_copy_and_preserves_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "pipeline.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    candidate = tmp_path / "candidate"
    (candidate / "excel").mkdir(parents=True)
    workbook = candidate / "excel" / "portfolio-report.xlsx"
    workbook.write_bytes(b"retained workbook")
    (candidate / "workbook-contract.json").write_text("{}", encoding="utf-8")
    (candidate / "xlsx-job.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "open",
                        "read_only": False,
                        "workbook_path": str(workbook.resolve()),
                    },
                    {"type": "recalc", "mode": "full_rebuild"},
                    {
                        "type": "save_as",
                        "output_path": str(workbook.resolve()),
                        "overwrite": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime, cli = tmp_path / "python.exe", tmp_path / "cli.py"
    runtime.write_bytes(b"runtime")
    cli.write_bytes(b"cli")
    monkeypatch.setattr(qc, "XLSX_RUNTIME", runtime)
    monkeypatch.setattr(qc, "XLSX_CLI", cli)
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    copied_jobs: list[str] = []
    monkeypatch.setattr(
        qc, "_native_workbook_checks", lambda _path: {"parity_sha256": "e" * 64}
    )

    def run(command: Any, **_kwargs: Any) -> None:
        rendered = tuple(command)
        if "run" in rendered:
            copied_jobs.append(Path(rendered[3]).read_text(encoding="utf-8"))
            Path(rendered[rendered.index("--result") + 1]).write_text(
                json.dumps({"ok": True, "final_state": "SUCCEEDED"}), encoding="utf-8"
            )

    monkeypatch.setattr(qc, "_run_checked", run)
    evidence = tmp_path / "evidence"

    assert (
        qc.main(
            [
                "--profile",
                "workstation",
                "--spec",
                str(spec_path),
                "--candidate-dir",
                str(candidate),
                "--evidence-dir",
                str(evidence),
            ]
        )
        == 0
    )
    assert workbook.read_bytes() == b"retained workbook"
    assert str(candidate.resolve()) not in copied_jobs[0]
    # System.Text.Json requires the polymorphic discriminator first.
    copied_steps = json.loads(copied_jobs[0])["steps"]
    assert all(next(iter(step)) == "type" for step in copied_steps)
    assert len(list(evidence.glob("*.json"))) == 1


def test_workstation_rejects_xlsx_job_write_escape(tmp_path: Path) -> None:
    candidate, disposable = tmp_path / "candidate", tmp_path / "disposable"
    candidate.mkdir()
    disposable.mkdir()

    with pytest.raises(qc.QualityControlFailure, match="escapes candidate"):
        qc._rewrite_job(
            {"output_path": str(tmp_path / "outside.xlsx")}, candidate, disposable
        )


def test_release_preflight_emits_measured_content_addressed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    spec.state_root = tmp_path / "private-stage"
    evidence = tmp_path / "audit" / "coding" / "execution-evidence"
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    monkeypatch.setattr(
        qc, "_candidate_qa", lambda *_args: {"financial_outcomes_status": "PASS"}
    )

    assert (
        qc.main(
            [
                "--profile",
                "release-preflight",
                "--spec",
                str(spec_path),
                "--evidence-dir",
                str(evidence),
            ]
        )
        == 0
    )

    receipt_path = next(evidence.glob("*.json"))
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt_path.stem == receipt["receipt_id"]
    assert receipt["production_state_before"] == receipt["production_state_after"]
    assert receipt["plan"]["roots"] == [
        {"generation_id": "2" * 64, "root_id": "portfolio"}
    ]


def test_release_preflight_emits_commit_authorization_bound_to_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    spec.state_root = tmp_path / "private-stage"
    authorization = tmp_path / "authority" / "commit-authorization.json"
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    monkeypatch.setattr(qc, "_candidate_qa", lambda *_args: {})

    argv = [
        "--profile",
        "release-preflight",
        "--spec",
        str(spec_path),
        "--evidence-dir",
        str(tmp_path / "evidence"),
        "--emit-commit-authorization",
        str(authorization),
    ]
    assert qc.main(argv) == 0

    from quant_engine.core import GateEvidence

    evidence = GateEvidence.from_dict(json.loads(authorization.read_bytes()))
    assert evidence.gate_id == "owner-publication-authorization"
    assert evidence.evidence_sha256 == qc._spec_sha256(spec)
    # Re-running the same passing preflight is idempotent.
    assert qc.main(argv) == 0


def test_release_preflight_writes_no_authorization_when_a_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    authorization = tmp_path / "commit-authorization.json"
    snapshots = [{"authority": {"exists": False}}, {"authority": {"exists": True}}]
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    monkeypatch.setattr(qc, "_candidate_qa", lambda *_args: {})
    monkeypatch.setattr(qc, "_production_snapshot", lambda _spec: snapshots.pop(0))

    assert (
        qc.main(
            [
                "--profile",
                "release-preflight",
                "--spec",
                str(spec_path),
                "--evidence-dir",
                str(tmp_path / "evidence"),
                "--emit-commit-authorization",
                str(authorization),
            ]
        )
        == 1
    )
    assert not authorization.exists()


def test_release_preflight_refuses_an_authorization_for_another_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    spec.state_root = tmp_path / "private-stage"
    authorization = tmp_path / "commit-authorization.json"
    authorization.write_bytes(
        json.dumps(
            {"evidence_sha256": "f" * 64, "gate_id": "owner-publication-authorization"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    monkeypatch.setattr(qc, "_candidate_qa", lambda *_args: {})

    assert (
        qc.main(
            [
                "--profile",
                "release-preflight",
                "--spec",
                str(spec_path),
                "--evidence-dir",
                str(tmp_path / "evidence"),
                "--emit-commit-authorization",
                str(authorization),
            ]
        )
        == 1
    )
    assert json.loads(authorization.read_bytes())["evidence_sha256"] == "f" * 64


def test_release_preflight_rejects_pointer_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    snapshots = [{"authority": {"exists": False}}, {"authority": {"exists": True}}]
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    monkeypatch.setattr(qc, "_candidate_qa", lambda *_args: {})
    monkeypatch.setattr(qc, "_production_snapshot", lambda _spec: snapshots.pop(0))

    assert (
        qc.main(
            [
                "--profile",
                "release-preflight",
                "--spec",
                str(spec_path),
                "--evidence-dir",
                str(tmp_path / "evidence"),
            ]
        )
        == 1
    )


def test_post_release_rejects_counterfeit_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    preflight = _write_preflight(evidence, spec)
    value = json.loads(preflight.read_bytes())
    value["plan"]["roots"][0]["generation_id"] = "3" * 64
    preflight.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))

    assert (
        qc.main(
            [
                "--profile",
                "post-release-verify",
                "--spec",
                str(spec_path),
                "--preflight-receipt",
                str(preflight),
                "--evidence-dir",
                str(evidence),
            ]
        )
        == 1
    )


def test_post_release_rejects_non_committed_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    preflight = _write_preflight(evidence, spec)
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))

    def reject(_spec: Any) -> Any:
        raise qc.QualityControlFailure("authoritative release journal is not COMMITTED")

    monkeypatch.setattr(qc, "_read_committed_release", reject)

    assert (
        qc.main(
            [
                "--profile",
                "post-release-verify",
                "--spec",
                str(spec_path),
                "--preflight-receipt",
                str(preflight),
                "--evidence-dir",
                str(evidence),
            ]
        )
        == 1
    )


def test_post_release_uses_existing_release_inputs_without_extra_qa_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path)
    (spec.coordination_root / "CURRENT_RELEASE.json").write_text("{}")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    preflight = _write_preflight(evidence, spec)
    monkeypatch.setattr(qc, "_load_and_validate_spec", lambda _path: (spec, _proof()))
    monkeypatch.setattr(qc, "_read_committed_release", lambda _spec: object())
    monkeypatch.setattr(qc, "_verify_committed_manifest", lambda *_args: {})
    assert (
        qc.main(
            [
                "--profile",
                "post-release-verify",
                "--spec",
                str(spec_path),
                "--preflight-receipt",
                str(preflight),
                "--evidence-dir",
                str(evidence),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
