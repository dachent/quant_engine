"""Acceptance MVP: validate one control-panel generation (corrective program WS5).

    python tools/validate_generation.py <generation_dir>
        [--expected expected-baseline.json] [--out findings.json]

Reads the generation's published outputs (report payload, panel payload, shell and
candidate record), runs every deterministic acceptance check in
``acceptance/validate/checks.py`` and writes the findings document defined by
``state/briefs/findings-contract.md``:

    {"schema": "qe-validate-generation/1", "generation_id", "generation_dir",
     "validator_commit", "expected_baseline_sha256", "summary", "findings"}

Exit status: 0 = no ``BLOCKER_*`` finding, 1 = at least one blocker, 2 = usage, IO or
contract error. The validator never writes inside a generation directory; without
``--out`` it writes ``<generations-root>/../validation/<generation_id>/findings.json``.

Standard library only. It deliberately imports none of the production report or
panel builders: it judges their outputs and must not share their code (plan v5 4.5A).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from acceptance.validate.checks import (  # noqa: E402
    BLOCKERS,
    CLASSES,
    Context,
    Finding,
    expected_exclusions,
    findings_document,
    rollup,
    run_checks,
    to_date,
)
from acceptance.validate.generation import (  # noqa: E402
    GenerationError,
    is_placeholder,
    load_generation,
)

EXIT_OK = 0
EXIT_BLOCKERS = 1
EXIT_ERROR = 2


class ContractError(Exception):
    """An input violates its contract; the runner exits with status 2."""


def _read_json(path: Path, label: str) -> tuple[Any, str]:
    if not path.is_file():
        raise ContractError(f"{label} does not exist: {path}")
    if is_placeholder(path):
        raise ContractError(
            f"{label} is a cloud placeholder (make it resident): {path}"
        )
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def load_expected(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str]:
    """Rows of an expected baseline (plan 4.5D) or a derivation output file."""

    value, digest = _read_json(path, "expected baseline")
    not_derivable: list[dict[str, Any]] = []
    cutoff: Any = None
    rows: Any
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = next(
            (value[key] for key in ("metrics", "expectations", "rows") if key in value),
            None,
        )
        raw_not_derivable = value.get("not_derivable") or []
        if not isinstance(raw_not_derivable, list):
            raise ContractError("expected baseline not_derivable must be a list")
        not_derivable = [item for item in raw_not_derivable if isinstance(item, dict)]
        cutoff = value.get("cutoff") or value.get("declared_cutoff")
    else:
        raise ContractError("expected baseline must be a JSON list or object")
    if not isinstance(rows, list):
        raise ContractError("expected baseline has no metrics/expectations/rows list")
    checked: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractError(f"expected baseline row {position} is not an object")
        if not isinstance(row.get("metric_id"), str) or not isinstance(
            row.get("entity"), str
        ):
            raise ContractError(
                f"expected baseline row {position} needs string metric_id and entity"
            )
        if "expected" not in row and "value" not in row:
            raise ContractError(
                f"expected baseline row {position} has no expected/value field"
            )
        checked.append(row)
    if cutoff is not None and not isinstance(cutoff, str):
        raise ContractError("expected baseline cutoff must be an ISO date string")
    return checked, not_derivable, cutoff, digest


def load_corrections(path: Path) -> list[dict[str, Any]]:
    value, _ = _read_json(path, "corrections file")
    records = value.get("corrections") if isinstance(value, dict) else value
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ContractError("corrections file must be a list of objects")
    return records


def validator_commit() -> str:
    override = os.environ.get("QE_VALIDATOR_COMMIT")
    if override:
        return override
    here = Path(__file__).resolve().parent
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=here,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    commit = completed.stdout.strip()
    return commit if len(commit) == 40 else "UNKNOWN"


def default_out(gen_dir: Path, generation_id: str) -> Path | None:
    if gen_dir.parent.name != "generations":
        return None
    return gen_dir.parent.parent / "validation" / generation_id / "findings.json"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(content.encode("utf-8"))
    os.replace(temporary, path)


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _print_summary(findings: Sequence[Finding], out: Path, generation_id: str) -> None:
    counts = {klass: 0 for klass in CLASSES}
    for finding in findings:
        counts[finding.klass] += 1
    print(f"validate_generation: generation {generation_id}")
    print("  " + "  ".join(f"{klass}={count}" for klass, count in counts.items()))
    details = rollup(findings)
    print(
        "  defect ids: "
        + ", ".join(f"{k}({v})" for k, v in details["by_defect"].items())
    )
    for finding in findings:
        if finding.klass in BLOCKERS:
            print(
                _ascii(
                    f"  {finding.klass} {finding.check_id} [{finding.entity}] {finding.message}"
                )
            )
    print(f"  findings written to {out}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one control-panel generation (acceptance MVP)."
    )
    parser.add_argument("generation_dir", type=Path)
    parser.add_argument("--expected", type=Path, help="expected-baseline JSON")
    parser.add_argument("--out", type=Path, help="findings document path")
    parser.add_argument("--details-out", type=Path, help="findings with messages")
    parser.add_argument("--cutoff", help="declared financial cutoff (YYYY-MM-DD)")
    parser.add_argument("--crosswalk", type=Path, help="account/strategy crosswalk")
    parser.add_argument("--release-pointer", type=Path, help="CURRENT_RELEASE.json")
    parser.add_argument("--corrections", type=Path, help="correction records (#326)")
    parser.add_argument(
        "--independent-no-oracle",
        action="store_true",
        help="validate against no expected baseline (requires --cutoff, forbids "
        "--expected); the only way to check a cutoff the baseline does not declare",
    )
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="do not look outside the generation for pointer/crosswalk/release",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_ERROR
    try:
        expected: list[dict[str, Any]] | None = None
        not_derivable: list[dict[str, Any]] = []
        expected_cutoff: str | None = None
        expected_sha: str | None = None
        if args.independent_no_oracle and (
            args.expected is not None or not args.cutoff
        ):
            raise ContractError(
                "--independent-no-oracle requires --cutoff and cannot be combined "
                "with --expected"
            )
        if args.expected is not None:
            expected, not_derivable, expected_cutoff, expected_sha = load_expected(
                args.expected
            )
            # A baseline is valid only for the cutoff it declares: comparing its
            # rows to another period would present one period's expectations as
            # another period's acceptance.
            if args.cutoff and (
                to_date(expected_cutoff) is None
                or to_date(args.cutoff) != to_date(expected_cutoff)
            ):
                raise ContractError(
                    f"--cutoff {args.cutoff} does not match the expected baseline's "
                    f"declared cutoff ({expected_cutoff}); re-baseline the period or "
                    "run with --independent-no-oracle and no --expected"
                )
        corrections = (
            load_corrections(args.corrections) if args.corrections is not None else None
        )
        generation = load_generation(
            args.generation_dir,
            discover=not args.no_discover,
            crosswalk_path=args.crosswalk,
            release_pointer_path=args.release_pointer,
        )
        generation_id = generation.manifest.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise ContractError("generation.json has no generation_id")
        cutoff_text, cutoff_source = next(
            (
                (value, source)
                for value, source in (
                    (args.cutoff, "--cutoff"),
                    (expected_cutoff, "expected baseline"),
                    (generation.report.get("calculation_cutoff_date"), "report"),
                    (generation.panel.get("meta", {}).get("portfolio_as_of"), "panel"),
                )
                if value
            ),
            (None, ""),
        )
        cutoff = to_date(cutoff_text)
        if cutoff is None:
            raise ContractError("no declared cutoff could be determined")
        out = args.out or default_out(generation.gen_dir, generation_id)
        if out is None:
            raise ContractError(
                "--out is required when the generation is not under a generations/ root"
            )
        if _inside(out, generation.gen_dir):
            raise ContractError("refusing to write findings inside the generation")
    except (ContractError, GenerationError) as exc:
        print(_ascii(f"validate_generation: ERROR: {exc}"), file=sys.stderr)
        return EXIT_ERROR
    context = Context(
        gen=generation,
        cutoff=cutoff,
        cutoff_source=cutoff_source,
        expected=expected,
        expected_not_derivable=not_derivable,
        corrections=corrections,
    )
    findings = run_checks(context)
    document = findings_document(
        generation_id=generation_id,
        generation_dir=str(generation.gen_dir),
        validator_commit=validator_commit(),
        expected_baseline_sha256=expected_sha,
        findings=findings,
    )
    try:
        _write_json(out, document)
        if args.details_out is not None:
            if _inside(args.details_out, generation.gen_dir):
                raise ContractError("refusing to write details inside the generation")
            _write_json(
                args.details_out,
                {
                    "generation_id": generation_id,
                    "cutoff": cutoff.isoformat(),
                    "cutoff_source": cutoff_source,
                    "mode": (
                        "INDEPENDENT_NO_ORACLE"
                        if args.independent_no_oracle
                        else "EXPECTED_BASELINE"
                        if expected is not None
                        else "NO_EXPECTED_BASELINE"
                    ),
                    "reference_notes": generation.external.notes,
                    "rollup": rollup(findings),
                    "expected_exclusions": expected_exclusions(context),
                    "findings": [
                        {**finding.as_json(), "message": finding.message}
                        for finding in findings
                    ],
                },
            )
    except (OSError, ContractError) as exc:
        print(_ascii(f"validate_generation: ERROR: {exc}"), file=sys.stderr)
        return EXIT_ERROR
    _print_summary(findings, out, generation_id)
    return EXIT_BLOCKERS if any(f.klass in BLOCKERS for f in findings) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
