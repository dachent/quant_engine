"""Pre-chain stage: layered history -> dependence -> strategy attribution (RELEASE-DRIVER-001, B5).

Replaces the hand script ``pre_chain.py`` of the R2 passes. It makes the same calls with the
same arguments as the attended preparation does for its history and dependence stages, so the
retained attribution binds the same authorities; what changes is where its inputs come from.
The cycle root, the evaluation date and the cycle identity are read from the resolved-cycle
file written by ``tools/release_cycle_manifest.py resolve`` (``docs/release-cycle-manifest.md``),
and every id is taken from the cycle's own outputs, never typed.

usage: release_pre_chain.py --resolved-cycle FILE --cycle DIR --selected-inputs FILE
                            --commit SHA [--baseline-id ID]

A stage killed part-way is restarted by running it again: an existing history is re-used, the
dependence output and the create-only attribution stage are re-verified, and a cycle identity
file that names another cycle manifest or commit refuses the run.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import date
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Sequence

RESOLVED_SCHEMA = "qe-release-resolved-cycle/1"
IDENTITY_NAME = "pre-chain-cycle.json"
IDENTITY_SCHEMA = "qe-release-pre-chain-cycle/1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


class PreChainError(RuntimeError):
    """A named refusal; the stage writes nothing further after it."""


def load_resolved_cycle(path: Path) -> dict[str, Any]:
    """The fields this stage reads, from a resolved-cycle file; anything else is refused."""

    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise PreChainError(f"resolved cycle is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != RESOLVED_SCHEMA:
        raise PreChainError(f"resolved cycle schema differs from {RESOLVED_SCHEMA}")
    scratch = value.get("scratch")
    identity = value.get("cycle_manifest_sha256")
    if not isinstance(scratch, str) or not os.path.isabs(scratch):
        raise PreChainError("resolved cycle has no absolute scratch root")
    if not isinstance(identity, str) or not _SHA256.fullmatch(identity):
        raise PreChainError("resolved cycle has no cycle_manifest_sha256")
    try:
        evaluated = date.fromisoformat(str(value.get("evaluated_on")))
    except ValueError as exc:
        raise PreChainError("resolved cycle evaluated_on is not a date") from exc
    return {
        "scratch": scratch,
        "evaluated_on": evaluated,
        "cycle_manifest_sha256": identity,
    }


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


def _inside(path: Path, root: Path) -> bool:
    child, base = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return base in child.parents


def _single_dir(directory: Path, what: str) -> Path:
    try:
        names = sorted(p.name for p in directory.iterdir() if p.is_dir())
    except OSError as exc:
        raise PreChainError(f"{what} is unavailable: {directory}") from exc
    if len(names) != 1:
        raise PreChainError(f"{what} must hold exactly one generation: {names}")
    return directory / names[0]


def _bind_cycle_identity(cycle: Path, identity: str, commit: str) -> None:
    """Pin the cycle manifest to the partial outputs, create-only; a second run must agree."""

    path = cycle / IDENTITY_NAME
    record = {
        "cycle_manifest_sha256": identity,
        "release_commit": commit,
        "schema_version": IDENTITY_SCHEMA,
    }
    if path.exists():
        try:
            existing = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise PreChainError(f"cycle identity file is unreadable: {path}") from exc
        if existing != record:
            raise PreChainError(
                f"pre-chain outputs belong to another cycle manifest or commit: {path}"
            )
        return
    handle, name = tempfile.mkstemp(dir=cycle, prefix=".pre-chain-cycle-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write((json.dumps(record, indent=1, sort_keys=True) + "\n").encode())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _load_modules() -> tuple[Any, Any, Any]:
    import portfolio_attended_preparation as prep
    import portfolio_attribution as attribution
    import portfolio_current_dependence as dependence

    return prep, dependence, attribution


def pre_chain(
    *,
    cycle: Path,
    selected_inputs: Path,
    baseline_gid: str,
    evaluated_on: date,
    commit: str,
    modules: tuple[Any, Any, Any] | None = None,
) -> int:
    prep, dependence, attribution = modules or _load_modules()
    repo = prep.ROOT
    contract = repo / "contracts" / "portfolio-financial-mvp-source-v1.json"
    policy = repo / "contracts" / "portfolio-dependence-policy-v1.json"
    selection = json.loads(selected_inputs.read_text(encoding="utf-8"))
    files = selection["files"]
    authorities = prep.selected_history_authorities(files)

    work = cycle / "pre"
    history_root_out = work / "history"
    if not history_root_out.exists():
        history_source = prep.rebuild_layered_history(
            selection, history_root_out, commit, contract, authorities
        )
    else:
        history_source = _single_dir(history_root_out, "existing layered history")
        try:
            prep.verify_selected_layered_history(history_source, contract, authorities)
        except (OSError, ValueError) as exc:
            # A kill during the history write leaves a generation that cannot verify and
            # whose truncated file the idempotent writer would refuse. The directory is
            # this stage's own output (bound to this cycle above), so rebuild it whole.
            print("rebuilding incomplete history", history_source, exc)
            if not _inside(history_root_out, cycle):
                raise PreChainError("history root is outside the cycle") from exc
            shutil.rmtree(history_root_out)
            history_source = prep.rebuild_layered_history(
                selection, history_root_out, commit, contract, authorities
            )
        else:
            print("reusing history", history_source)
    print("HISTORY", history_source)

    baseline_source = cycle / "baseline" / "generations" / baseline_gid
    ledger_root = Path(
        selection["builder_kwargs"]["provider_ledger_manifest_path"]
    ).parent
    selected = {
        "schema_version": 1,
        "history_generation_root": str(history_source.resolve()),
        "allocation_generation_root": str(baseline_source.resolve()),
        "provider_ledger_generation_root": str(ledger_root.resolve()),
        "financial_input_contract_path": str(contract.resolve()),
        "provider_admission_receipt_path": str(
            authorities["provider_admission_receipt_path"].resolve()
        ),
        "portfolio123_version_evidence_path": str(
            authorities["portfolio123_version_evidence_path"].resolve()
        ),
    }
    selected_path = work / "dependence-selected-inputs.json"
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    selected_path.write_text(
        json.dumps(selected, indent=1, sort_keys=True), encoding="utf-8", newline="\n"
    )
    result = dependence.write_source(
        work / "dependence",
        contract_path=contract,
        policy_path=policy,
        history_generation_root=history_source,
        allocation_generation_root=baseline_source,
        evaluated_on=evaluated_on,
        selected_inputs_path=selected_path,
    )
    dependence_root = Path(str(result["destination"]))
    dependence.verify_packaged_source(
        dependence_root,
        contract_path=contract,
        policy_path=policy,
        selected_inputs_path=selected_path,
    )
    print("DEPENDENCE", dependence_root)
    captured, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
        try:
            code = int(
                attribution.main(
                    [
                        "stage",
                        "--baseline-root",
                        str(cycle / "baseline"),
                        "--baseline-generation-id",
                        baseline_gid,
                        "--dependence-source-root",
                        str(dependence_root),
                        "--dependence-selected-inputs",
                        str(selected_path),
                        "--spec",
                        str(cycle / "attribution-spec.json"),
                        "--output-root",
                        str(cycle / "attribution"),
                    ]
                )
            )
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    print(code, captured.getvalue()[-600:], errors.getvalue()[-800:])
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-cycle", type=Path, required=True)
    parser.add_argument("--cycle", type=Path, required=True)
    parser.add_argument("--selected-inputs", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--baseline-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        resolved = load_resolved_cycle(args.resolved_cycle)
        cycle = args.cycle
        if not _same(cycle, Path(resolved["scratch"])):
            raise PreChainError(
                "cycle directory is not the resolved cycle's scratch root: "
                f"{cycle} != {resolved['scratch']}"
            )
        if not _COMMIT.fullmatch(args.commit):
            raise PreChainError("commit must be 40 lowercase hex characters")
        if not args.selected_inputs.is_file():
            raise PreChainError(f"selected inputs are absent: {args.selected_inputs}")
        generations = cycle / "baseline" / "generations"
        if args.baseline_id is None:
            baseline_gid = _single_dir(generations, "cycle baseline").name
        elif (generations / args.baseline_id).is_dir():
            baseline_gid = args.baseline_id
        else:
            raise PreChainError(
                f"cycle baseline generation is absent: {args.baseline_id}"
            )
        if not (cycle / "attribution-spec.json").is_file():
            raise PreChainError("cycle attribution spec is absent")
        _bind_cycle_identity(cycle, resolved["cycle_manifest_sha256"], args.commit)
        return pre_chain(
            cycle=cycle,
            selected_inputs=args.selected_inputs,
            baseline_gid=baseline_gid,
            evaluated_on=resolved["evaluated_on"],
            commit=args.commit,
        )
    except PreChainError as exc:
        print(f"release-pre-chain: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
