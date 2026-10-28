"""Run the finite portfolio quality-control profiles over existing checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
XLSX_SKILL_ROOT = Path(r"C:\Users\BorisVaisman\.codex\skills\xlsx-win")
XLSX_RUNTIME = Path(r"C:\Tools\xlsx-win\.venv\Scripts\python.exe")
XLSX_CLI = XLSX_SKILL_ROOT / "control_plane" / "cli.py"
# Match the existing Quality workflow's 20-minute job bound.
COMMAND_TIMEOUT_SECONDS = 20 * 60
PROFILES = ("ci", "pre-pr", "workstation", "release-preflight", "post-release-verify")
CI_TARGETS = (
    "tests/core/test_portfolio_mvp2_vertical.py",
    "tests/core/test_portfolio_mvp2_product.py",
    "tests/core/test_portfolio_financial_outcomes.py",
    "tests/core/test_portfolio_financial_mvp_contract.py",
    "tests/portfolio_analytics/test_portfolio_invested_capital.py",
    "tests/portfolio_attribution",
    "tests/portfolio_operations",
)
REQUIRED_OUTCOMES = (
    "AS_HELD_COMPOSITE_HWM",
    "INVESTED_CAPITAL",
    "DECOMPOSITION",
    "DEPENDENCE",
)
# The coordinated commit requires this gate; see core.pipeline.
COMMIT_AUTHORIZATION_GATE_ID = "owner-publication-authorization"


class PrerequisiteUnavailable(RuntimeError):
    """A named environmental or input prerequisite is unavailable."""


class QualityControlFailure(RuntimeError):
    """A check ran and rejected the supplied candidate or repository state."""


def _regular_file(path: Path, name: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PrerequisiteUnavailable(f"{name}: {path}")
    return path


def _regular_directory(path: Path, name: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise PrerequisiteUnavailable(f"{name}: {path}")
    return path


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path = REPOSITORY_ROOT,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> str:
    try:
        completed = subprocess.run(
            tuple(command),
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise PrerequisiteUnavailable(f"executable unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise QualityControlFailure(
            f"check exceeded {timeout_seconds} seconds: {' '.join(command)}"
        ) from exc
    if completed.returncode:
        rendered = completed.stdout.strip()
        raise QualityControlFailure(
            f"check failed ({completed.returncode}): {' '.join(command)}"
            + (f"\n{rendered}" if rendered else "")
        )
    return completed.stdout


def _load_and_validate_spec(path: Path, *, profile: str) -> tuple[Any, dict[str, Any]]:
    _regular_file(path, "pipeline spec")
    try:
        from quant_engine.core.pipeline import PipelineSpec
        from portfolio_financial_outcomes import validate_financial_outcomes

        spec = PipelineSpec.from_json_bytes(path.read_bytes())
        proof = validate_financial_outcomes(
            spec, allow_synthetic=False, caller=f"portfolio_qc.{profile}"
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise PrerequisiteUnavailable("financial outcome validator") from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise QualityControlFailure(
            f"financial outcome validation failed: {exc}"
        ) from exc
    if proof.get("status") != "PASS":
        raise QualityControlFailure("financial outcome validator did not return PASS")
    return spec, proof


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def _spec_sha256(spec: Any) -> str:
    value = str(getattr(spec, "spec_sha256", "") or "")
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise QualityControlFailure("pipeline spec identity is unavailable")
    return value


def _within(path: Path, boundary: Path) -> bool:
    try:
        path.resolve().relative_to(boundary.resolve())
    except ValueError:
        return False
    return True


def _evidence_directory(path: Path | None) -> Path:
    if path is None:
        raise PrerequisiteUnavailable("explicit evidence directory")
    resolved = path.resolve()
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_dir()):
        raise QualityControlFailure("evidence directory is not a regular directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_receipt(evidence_dir: Path, payload: Mapping[str, Any]) -> Path:
    identity = hashlib.sha256(_canonical_bytes(dict(payload))).hexdigest()
    content = _canonical_bytes({**payload, "receipt_id": identity})
    path = evidence_dir / f"{identity}.json"
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise QualityControlFailure("content-addressed receipt collision") from None
    return path


def _write_commit_authorization(path: Path, spec_sha256: str) -> None:
    """Bind publication authority to the spec this preflight just verified.

    The coordinated commit requires an owner-publication-authorization gate
    whose evidence hash equals the spec hash. Emitting it here, only after every
    preflight check has passed, makes the authority a product of verification
    rather than a separate manual attestation: no passing preflight, no
    authorization, and an authorization can never cover a different spec.
    """
    content = _canonical_bytes(
        {"evidence_sha256": spec_sha256, "gate_id": COMMIT_AUTHORIZATION_GATE_ID}
    )
    if path.is_symlink():
        raise QualityControlFailure("commit authorization path is linked")
    if path.exists():
        if not path.is_file():
            raise QualityControlFailure("commit authorization path is not a file")
        if path.read_bytes() != content:
            raise QualityControlFailure(
                "existing commit authorization covers a different spec"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _read_preflight(path: Path, spec: Any) -> dict[str, Any]:
    try:
        value = json.loads(_regular_file(path, "preflight receipt").read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualityControlFailure("preflight receipt is invalid") from exc
    if not isinstance(value, dict):
        raise QualityControlFailure("preflight receipt is invalid")
    identity = value.pop("receipt_id", None)
    expected = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    if (
        identity != expected
        or path.stem != expected
        or value.get("kind") != "portfolio-release-preflight-v1"
        or value.get("status") != "PASS"
        or value.get("spec_sha256") != _spec_sha256(spec)
        or value.get("production_state_before") != value.get("production_state_after")
    ):
        raise QualityControlFailure("preflight receipt identity differs")
    return {**value, "receipt_id": identity}


def _file_state(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise QualityControlFailure(f"production state path is linked: {path.name}")
    if not path.exists():
        return {"exists": False}
    if not path.is_file():
        raise QualityControlFailure(f"production state path is not a file: {path.name}")
    return {"exists": True, "sha256": _sha256(path), "size_bytes": path.stat().st_size}


def _production_snapshot(spec: Any) -> dict[str, object]:
    return {
        "authority": _file_state(Path(spec.coordination_root) / "CURRENT_RELEASE.json"),
        "companions": {
            item.companion_id: _file_state(
                Path(spec.roots[item.root_id]) / item.relative_path
            )
            for item in sorted(spec.companions, key=lambda row: row.companion_id)
        },
        "roots": {
            root_id: _file_state(Path(spec.roots[root_id]) / "CURRENT.json")
            for root_id in sorted(spec.release_generations)
        },
    }


def _expected_plan(spec: Any) -> dict[str, object]:
    companions = []
    for item in sorted(spec.companions, key=lambda row: row.companion_id):
        source = _regular_file(item.source_path, f"companion {item.companion_id}")
        if _sha256(source) != item.sha256:
            raise QualityControlFailure(
                f"companion source differs: {item.companion_id}"
            )
        companions.append(
            {
                "companion_id": item.companion_id,
                "proposed_sha256": item.sha256,
                "relative_path": item.relative_path,
                "root_id": item.root_id,
            }
        )
    return {
        "companions": companions,
        "roots": [
            {"generation_id": generation_id, "root_id": root_id}
            for root_id, generation_id in sorted(spec.release_generations.items())
        ],
    }


def _candidate_qa(spec: Any, proof: Mapping[str, Any]) -> dict[str, object]:
    try:
        from quant_engine.core.pipeline import plan_pipeline

        plan = plan_pipeline(spec)
    except (ImportError, ModuleNotFoundError) as exc:
        raise PrerequisiteUnavailable("pipeline planner") from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise QualityControlFailure(f"pipeline plan validation failed: {exc}") from exc
    expected = cast(dict[str, list[dict[str, str]]], _expected_plan(spec))
    expected_effects = [
        *[
            {
                "generation_id": row["generation_id"],
                "root_id": row["root_id"],
                "type": "CURRENT_POINTER",
            }
            for row in expected["roots"]
        ],
        *[
            {
                "companion_id": row["companion_id"],
                "relative_path": row["relative_path"],
                "root_id": row["root_id"],
                "type": "COMPANION",
            }
            for row in expected["companions"]
        ],
    ]
    if plan.get("commit_effects") != expected_effects or plan.get(
        "spec_sha256"
    ) != _spec_sha256(spec):
        raise QualityControlFailure("pipeline planner release effects differ")
    gates = []
    for gate in sorted(spec.gates, key=lambda row: row.gate_id):
        if (
            _sha256(_regular_file(gate.path, f"pipeline gate {gate.gate_id}"))
            != gate.sha256
        ):
            raise QualityControlFailure(f"pipeline gate differs: {gate.gate_id}")
        gates.append({"gate_id": gate.gate_id, "sha256": gate.sha256})
    return {
        "financial_outcomes": _financial_evidence(proof),
        "gates": gates,
        "planner_spec_sha256": plan["spec_sha256"],
    }


def _financial_evidence(proof: Mapping[str, Any]) -> dict[str, object]:
    from portfolio_financial_outcomes import financial_outcomes_semantic_identity

    if (
        proof.get("status") != "PASS"
        or tuple(proof.get("outcome_ids", ())) != REQUIRED_OUTCOMES
    ):
        raise QualityControlFailure("financial outcome identity set differs")
    artifact_sha256s = proof.get("artifact_sha256s")
    selected_cells = proof.get("selected_cells")
    consumers = proof.get("consumers")
    capital_result_id = proof.get("capital_result_id")
    work_counters = proof.get("work_counters")
    if (
        not isinstance(artifact_sha256s, dict)
        or not artifact_sha256s
        or not isinstance(selected_cells, dict)
        or not selected_cells
        or not isinstance(consumers, dict)
        or not consumers
        or not isinstance(capital_result_id, str)
        or len(capital_result_id) != 64
        or not isinstance(work_counters, dict)
        or set(work_counters)
        != {
            "model_fit_calls",
            "grid_search_calls",
            "correlation_build_calls",
            "full_rederivation_count",
            "receipt_reuse_count",
        }
        or any(
            not isinstance(value, int) or value < 0 for value in work_counters.values()
        )
    ):
        raise QualityControlFailure("financial outcome proof is incomplete")
    return {
        "artifact_sha256s": dict(sorted(artifact_sha256s.items())),
        "capital_result_id": capital_result_id,
        "financial_outcomes_sha256": financial_outcomes_semantic_identity(proof),
        "consumer_proof_sha256": hashlib.sha256(
            _canonical_bytes(consumers)
        ).hexdigest(),
        "outcome_states": {outcome_id: "AVAILABLE" for outcome_id in REQUIRED_OUTCOMES},
        "selected_cells_sha256": hashlib.sha256(
            _canonical_bytes(selected_cells)
        ).hexdigest(),
        "work_counters": dict(sorted(work_counters.items())),
    }


def _require_ci_targets() -> None:
    for target in CI_TARGETS:
        path = REPOSITORY_ROOT / target
        if not path.exists() or path.is_symlink():
            raise PrerequisiteUnavailable(f"CI required target: {target}")


def _ci() -> None:
    _require_ci_targets()
    _run_checked((sys.executable, "-m", "pytest", "-q", *CI_TARGETS))
    _run_checked(
        (
            sys.executable,
            str(TOOLS_ROOT / "check_repository_boundary.py"),
            "--root",
            str(REPOSITORY_ROOT),
            "--format",
            "json",
        )
    )


def _pre_pr(args: argparse.Namespace) -> None:
    if not args.expected_head:
        raise PrerequisiteUnavailable("expected exact head SHA")
    if args.pr is None:
        raise PrerequisiteUnavailable("pull request number")
    if not args.test_target:
        raise PrerequisiteUnavailable("bounded test target")
    if not args.changed_path:
        raise PrerequisiteUnavailable("changed path inventory")
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=REPOSITORY_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PrerequisiteUnavailable("Git exact-head read") from exc
    if completed.returncode:
        raise PrerequisiteUnavailable("Git exact-head read")
    if completed.stdout.strip() != args.expected_head:
        raise QualityControlFailure("repository HEAD differs from --expected-head")
    try:
        status = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=REPOSITORY_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PrerequisiteUnavailable("Git working-tree read") from exc
    if status.returncode:
        raise PrerequisiteUnavailable("Git working-tree read")
    if status.stdout.strip():
        raise QualityControlFailure("repository has uncommitted changes")
    _run_checked((sys.executable, "-m", "pytest", "-q", *args.test_target))
    python_paths = [path for path in args.changed_path if path.endswith(".py")]
    if python_paths:
        _run_checked((sys.executable, "-m", "ruff", "check", *python_paths))
        _run_checked((sys.executable, "-m", "ruff", "format", "--check", *python_paths))
    _run_checked(
        ("git", "diff", "--check", f"{args.expected_head}^", args.expected_head)
    )
    _run_checked(
        (
            sys.executable,
            str(TOOLS_ROOT / "portfolio_financial_mvp_contract.py"),
            "check",
        )
    )
    readiness = _run_checked(
        (sys.executable, str(TOOLS_ROOT / "merge_readiness.py"), str(args.pr))
    )
    try:
        remote = json.loads(readiness)
    except (TypeError, json.JSONDecodeError) as exc:
        raise QualityControlFailure("merge-readiness result is invalid") from exc
    if not isinstance(remote, dict) or remote.get("head") != args.expected_head:
        raise QualityControlFailure("PR head differs from --expected-head")


_PATH_KEYS = (
    "path",
    "file",
    "workbook",
    "output",
    "result",
    "events",
    "target",
    "source",
)


def _rewrite_job(value: Any, candidate: Path, disposable: Path, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            item_key: _rewrite_job(item, candidate, disposable, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_job(item, candidate, disposable, key) for item in value]
    if not isinstance(value, str) or not any(
        token in key.lower() for token in _PATH_KEYS
    ):
        return value
    item = Path(value)
    source = item.resolve() if item.is_absolute() else (candidate / item).resolve()
    if not _within(source, candidate):
        raise QualityControlFailure(f"xlsx-win job path escapes candidate: {key}")
    destination = (disposable / source.relative_to(candidate.resolve())).resolve()
    if not _within(destination, disposable):
        raise QualityControlFailure(f"xlsx-win job path escapes disposable copy: {key}")
    return str(destination)


def _inventory(directory: Path) -> dict[str, object]:
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise QualityControlFailure("candidate inventory contains a symlink")
        if path.is_file():
            rows.append(
                {
                    "relative_path": path.relative_to(directory).as_posix(),
                    "sha256": _sha256(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return {
        "file_count": len(rows),
        "inventory_sha256": hashlib.sha256(_canonical_bytes(rows)).hexdigest(),
    }


def _native_workbook_checks(candidate: Path) -> dict[str, object]:
    import portfolio_report as report

    workbook = candidate / "excel" / report.WORKBOOK_NAME
    try:
        structure = report.verify_workbook(workbook)
        parity = report._workbook_parity_receipt(
            (candidate / report.REPORT_DATA_NAME).read_bytes(), workbook.read_bytes()
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise QualityControlFailure(
            f"native workbook financial checks failed: {exc}"
        ) from exc
    return {
        "workbook_sha256": _sha256(workbook),
        "structure_sha256": hashlib.sha256(_canonical_bytes(structure)).hexdigest(),
        "parity_sha256": hashlib.sha256(_canonical_bytes(parity)).hexdigest(),
    }


def _workstation(args: argparse.Namespace) -> Path:
    # This profile is exclusively an xlsx-win/desktop-Excel route.  Workbook
    # execution is permanently held for the current MVP, so reject the
    # profile before resolving any candidate, runtime, or evidence path.
    raise PrerequisiteUnavailable(
        "workbook-only workstation profile is permanently held"
    )


def _retired_workstation(args: argparse.Namespace) -> Path:
    if args.spec is None:
        raise PrerequisiteUnavailable("pipeline spec")
    if args.candidate_dir is None:
        raise PrerequisiteUnavailable("complete candidate directory")
    spec, proof = _load_and_validate_spec(args.spec, profile="workbook-only")
    evidence_dir = _evidence_directory(args.evidence_dir)
    candidate = _regular_directory(
        args.candidate_dir, "complete candidate directory"
    ).resolve()
    _regular_file(candidate / "excel" / "portfolio-report.xlsx", "candidate workbook")
    _regular_file(candidate / "workbook-contract.json", "workbook contract")
    job_path = _regular_file(candidate / "xlsx-job.json", "xlsx-win job")
    _regular_file(XLSX_RUNTIME, "xlsx-win runtime")
    _regular_file(XLSX_CLI, "xlsx-win CLI")
    before = _inventory(candidate)
    with tempfile.TemporaryDirectory(
        prefix="portfolio-qc-", dir=evidence_dir
    ) as temporary:
        disposable = Path(temporary) / "candidate"
        shutil.copytree(candidate, disposable)
        try:
            job = json.loads(job_path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QualityControlFailure("xlsx-win job is invalid") from exc
        copied_job = disposable / "xlsx-job.json"
        # Preserve the producer's discriminator-first step order for System.Text.Json.
        copied_job.write_bytes(
            json.dumps(
                _rewrite_job(job, candidate, disposable),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        result, events = (
            disposable / "xlsx-result.json",
            disposable / "xlsx-events.jsonl",
        )
        _run_checked(
            (
                str(XLSX_RUNTIME),
                str(XLSX_CLI),
                "run",
                str(copied_job),
                "--events",
                str(events),
                "--result",
                str(result),
                "--hard-timeout-seconds",
                "300",
            ),
            timeout_seconds=360,
        )
        _run_checked(
            (
                str(XLSX_RUNTIME),
                str(XLSX_CLI),
                "validate-contract",
                str(disposable / "excel" / "portfolio-report.xlsx"),
                str(disposable / "workbook-contract.json"),
            ),
            timeout_seconds=360,
        )
        try:
            outcome = json.loads(_regular_file(result, "xlsx-win result").read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QualityControlFailure("xlsx-win result is invalid") from exc
        if outcome.get("ok") is not True or outcome.get("final_state") != "SUCCEEDED":
            raise QualityControlFailure("native Excel validation did not succeed")
        native = _native_workbook_checks(disposable)
    after = _inventory(candidate)
    if after != before:
        raise QualityControlFailure("retained candidate changed during workstation QA")
    return _write_receipt(
        evidence_dir,
        {
            "candidate_inventory_after": after,
            "candidate_inventory_before": before,
            "financial_outcomes": _financial_evidence(proof),
            "kind": "portfolio-workstation-qa-v1",
            "native_excel_final_state": "SUCCEEDED",
            "native_financial_checks": native,
            "spec_sha256": _spec_sha256(spec),
            "status": "PASS",
        },
    )


FACT_ADMISSION_GATE_ID = "financial-mvp-provider-catalog-authority"


def _fact_admission(spec: Any) -> dict[str, Any]:
    """Run the dropped-fact gate on the candidate provider ledger (#535).

    The candidate ledger is the spec gate whose bytes ``_candidate_qa`` already
    pinned. Provider catalogs are read from the workspace that holds the
    coordination root (``<workspace>/dataPortfolio/<coordination>``). Every
    provider the ledger binds must be checked: a dropped or changed fact, a
    provider whose catalog is not on disk, or a ledger with nothing checkable
    fails the preflight, so an unchecked provider can never ride on another's PASS.
    """

    ledger = next(
        (gate.path for gate in spec.gates if gate.gate_id == FACT_ADMISSION_GATE_ID),
        None,
    )
    if ledger is None:
        return {"status": "NOT_APPLICABLE"}
    workspace = Path(spec.coordination_root).parents[1]
    output = _run_checked(
        (
            sys.executable,
            str(REPOSITORY_ROOT / "tools" / "check_fact_admission.py"),
            "--workspace",
            str(workspace),
            "--ledger",
            str(ledger),
        )
    )
    lines = output.strip().splitlines()
    providers = {
        name: state.split(" ", 1)[0]
        for name, _, state in (line.partition(": ") for line in lines[:-1])
        if state
    }
    unchecked = sorted(name for name, state in providers.items() if state != "PASS")
    if lines[-1] != "fact-admission: PASS" or unchecked or not providers:
        raise QualityControlFailure(
            "fact admission gate did not check every bound provider: "
            + (", ".join(unchecked) or lines[-1])
        )
    return {"providers": dict(sorted(providers.items())), "status": "PASS"}


def _release_preflight(args: argparse.Namespace) -> Path:
    if args.spec is None:
        raise PrerequisiteUnavailable("pipeline spec")
    spec, proof = _load_and_validate_spec(args.spec, profile="release-preflight")
    evidence_dir = _evidence_directory(args.evidence_dir)
    before = _production_snapshot(spec)
    qa, plan = _candidate_qa(spec, proof), _expected_plan(spec)
    qa["fact_admission"] = _fact_admission(spec)
    after = _production_snapshot(spec)
    if after != before:
        raise QualityControlFailure("production state changed during release preflight")
    receipt = _write_receipt(
        evidence_dir,
        {
            "candidate_qa": qa,
            "kind": "portfolio-release-preflight-v1",
            "plan": plan,
            "production_state_after": after,
            "production_state_before": before,
            "spec_sha256": _spec_sha256(spec),
            "status": "PASS",
        },
    )
    if args.emit_commit_authorization is not None:
        _write_commit_authorization(args.emit_commit_authorization, _spec_sha256(spec))
    return receipt


def _read_committed_release(spec: Any) -> Any:
    try:
        from quant_engine.core import read_current_release

        return read_current_release(
            spec.coordination_root,
            {root_id: spec.roots[root_id] for root_id in spec.release_generations},
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise PrerequisiteUnavailable("coordinated release reader") from exc
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise QualityControlFailure(
            f"committed release authority failed: {exc}"
        ) from exc


def _verify_committed_manifest(spec: Any, manifest: Any) -> dict[str, object]:
    if manifest.code_commit != spec.code_commit:
        raise QualityControlFailure("committed release code commit differs")
    targets = {item.root_id: item for item in manifest.targets}
    if set(targets) != set(spec.release_generations):
        raise QualityControlFailure("committed release root set differs")
    for root_id, generation_id in spec.release_generations.items():
        if targets[root_id].proposed_generation_id != generation_id:
            raise QualityControlFailure(f"committed generation differs: {root_id}")
    companions = {item.companion_id: item for item in manifest.companions}
    if set(companions) != {item.companion_id for item in spec.companions}:
        raise QualityControlFailure("committed companion set differs")
    for expected in spec.companions:
        observed = companions[expected.companion_id]
        if (
            observed.root_id != expected.root_id
            or observed.relative_path != expected.relative_path
            or observed.proposed_sha256 != expected.sha256
        ):
            raise QualityControlFailure(
                f"committed companion differs: {expected.companion_id}"
            )
    gates = {item.gate_id: item.evidence_sha256 for item in manifest.gates}
    for expected in spec.gates:
        if gates.get(expected.gate_id) != expected.sha256:
            raise QualityControlFailure(f"committed gate differs: {expected.gate_id}")
    expected_plan = _expected_plan(spec)
    return {
        "code_commit": manifest.code_commit,
        "companions": expected_plan["companions"],
        "release_id": manifest.release_id,
        "roots": expected_plan["roots"],
    }


def _post_release(args: argparse.Namespace) -> Path:
    if args.spec is None:
        raise PrerequisiteUnavailable("pipeline spec")
    if args.preflight_receipt is None:
        raise PrerequisiteUnavailable("accepted preflight receipt")
    spec, proof = _load_and_validate_spec(args.spec, profile="post-release-verify")
    evidence_dir = _evidence_directory(args.evidence_dir)
    preflight = _read_preflight(args.preflight_receipt, spec)
    if preflight.get("plan") != _expected_plan(spec):
        raise QualityControlFailure("preflight receipt release plan differs")
    manifest = _read_committed_release(spec)
    committed = _verify_committed_manifest(spec, manifest)
    state = _production_snapshot(spec)
    authority = cast(Mapping[str, object], state["authority"])
    if authority.get("exists") is not True:
        raise QualityControlFailure("committed coordination authority is unavailable")
    if _production_snapshot(spec) != state:
        raise QualityControlFailure("production state changed during post-release QA")
    return _write_receipt(
        evidence_dir,
        {
            "committed_release": committed,
            "financial_outcomes": _financial_evidence(proof),
            "kind": "portfolio-post-release-verify-v1",
            "preflight_receipt_id": preflight["receipt_id"],
            "preflight_receipt_sha256": _sha256(args.preflight_receipt),
            "production_state": state,
            "spec_sha256": _spec_sha256(spec),
            "status": "PASS",
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one named portfolio quality-control profile."
    )
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--preflight-receipt", type=Path)
    parser.add_argument("--emit-commit-authorization", type=Path)
    parser.add_argument("--expected-head")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--test-target", action="append", default=[])
    parser.add_argument("--changed-path", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt: Path | None = None
    try:
        if args.profile == "ci":
            _ci()
        elif args.profile == "pre-pr":
            _pre_pr(args)
        elif args.profile == "workstation":
            receipt = _workstation(args)
        elif args.profile == "release-preflight":
            receipt = _release_preflight(args)
        else:
            receipt = _post_release(args)
    except PrerequisiteUnavailable as exc:
        print(
            json.dumps(
                {
                    "profile": args.profile,
                    "status": "BLOCKED",
                    "prerequisite": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    except QualityControlFailure as exc:
        print(
            json.dumps(
                {"profile": args.profile, "status": "FAILED", "reason": str(exc)},
                sort_keys=True,
            )
        )
        return 1
    output = {"profile": args.profile, "status": "PASS"}
    if receipt is not None:
        output.update({"receipt_file": receipt.name, "receipt_id": receipt.stem})
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
