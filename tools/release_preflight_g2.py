"""G2 product measurements: facts that exist only once the panel candidate has been built (#884, RULING-D G2).

G0 checks what can be read before any stage runs. Some refusals cannot be known then: payload size and the
validator's findings depend on the product (R3 refusals 12 and 17). G2 runs at the first moment the candidate
exists (end of ``attended``, before QA) and measures it with the consumers' own code: ``panel_payload_size``'s
``block_sizes``/``physical``/``check`` against the committed budget, and ``validate_generation.py`` itself.
Nothing here restates a limit or a rule. PASS and FAIL only; a row that cannot be measured is FAIL.

Receipt: ``{cycle}/preflight/g2.json`` (kind ``release-preflight-g2-v1``), bound to the run configuration, the code
identity and the candidate id. ``release_driver`` runs it as the builtin stage ``g2`` and a later stage declares
``needs_gate: ["g2"]``; the receipt and the rows are the same shape as G0's, so ``verify_receipt`` reads both.

Usage::

    python tools/release_preflight_g2.py g2 --config run.json [--graph G]
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import panel_payload_size as payload
import release_driver as driver
import release_preflight as g0

GATE = "G2"
KIND = "release-preflight-g2-v1"
STAGE_ID = "g2"
FINDINGS = "g2-findings.json"

RunCommand = Callable[[Sequence[str]], int]  # exit code of a command


def _run_command(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False, capture_output=True).returncode


class Env:
    """What a test replaces: running the validator."""

    def __init__(self, run_command: RunCommand = _run_command) -> None:
        self.run_command = run_command


Row = Callable[[driver.Run, Env], None]


def candidate_dir(run: driver.Run) -> Path:
    return Path(run.expand("{run}/stages/panel/candidates/{candidate_id}"))


def row_candidate(run: driver.Run, env: Env) -> None:
    """The product exists: a candidate directory with the payload the consumers read."""

    path = candidate_dir(run)
    if not path.is_dir():
        raise driver.Refusal(f"no panel candidate at {path.name}")
    if not (path / "data" / "dashboard_data.json").is_file():
        raise driver.Refusal("the candidate has no data/dashboard_data.json")


def row_payload_size(run: driver.Run, env: Env) -> None:
    """R3 refusal 17: a block over its budget was found only by the validator an hour into the run.

    The consumer's own measurement (``panel_payload_size``) against its committed budget, on this candidate."""

    path = candidate_dir(run)
    data = json.loads((path / "data" / "dashboard_data.json").read_bytes())
    findings = payload.check(
        payload.block_sizes(data),
        payload.physical(path),
        json.loads(payload.BUDGET_PATH.read_bytes()),
    )
    if findings:
        raise driver.Refusal(f"{len(findings)} over budget: {'; '.join(findings[:3])}")


def validator_argv(run: driver.Run) -> list[str]:
    """The stage's own validator command (one definition, in the stage graph), run on the candidate."""

    for stage in run.graph.get("stages", []):
        if stage.get("id") == STAGE_ID:
            template = stage.get("validator_argv")
            if not template:
                break
            return driver.stage_argv(run, stage, template)
    raise driver.Refusal("the g2 stage declares no validator_argv")


def row_validator_findings(run: driver.Run, env: Env) -> None:
    """R3 refusals 12 and 16 (second line): the validator's blockers on the candidate, found before QA.

    ``validate_generation.py`` exits 0 with no blocker, 1 with one, 2 when it cannot judge; only 0 passes."""

    argv = validator_argv(run)
    code = env.run_command(argv)
    if code != 0:
        raise driver.Refusal(
            f"validate_generation exited {code} on the candidate; findings: {FINDINGS}"
        )


ROWS: list[tuple[str, Row]] = [
    ("g2:candidate", row_candidate),
    ("g2:payload-size", row_payload_size),
    ("g2:validator-findings", row_validator_findings),
]


def _candidate_id(run: driver.Run) -> str:
    try:
        return run.expand("{candidate_id}")
    except driver.Refusal:
        return ""  # no candidate: the g2:candidate row is the FAIL, the receipt still records it


def run_g2(run: driver.Run, env: Env | None = None) -> dict[str, Any]:
    env = env or Env()
    rows: list[dict[str, str]] = []
    for row_id, fn in ROWS:
        try:
            fn(run, env)
            rows.append({"row": row_id, "status": "PASS", "detail": ""})
        except (driver.Refusal, OSError, ValueError, KeyError) as exc:
            rows.append({"row": row_id, "status": "FAIL", "detail": str(exc)[:400]})
    return {
        "kind": KIND,
        "gate": GATE,
        "config_sha256": g0.config_sha256(run),
        "code_commit": run.values["code_commit"],
        "candidate_id": _candidate_id(run),
        "rows": rows,
        "status": "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def builtin_g2(
    run: driver.Run, _target: Path, env: Env | None = None
) -> dict[str, Any]:
    """The driver's builtin stage: measure, write the receipt, refuse on FAIL (the stage then fails)."""

    receipt = run_g2(run, env)
    g0.write_receipt(run, receipt)
    failed = [
        f"{r['row']}: {r['detail']}" for r in receipt["rows"] if r["status"] != "PASS"
    ]
    if failed:
        raise driver.Refusal("G2 FAIL: " + " | ".join(failed))
    return {"status": "PASS"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("g2")
    cmd.add_argument("--config", type=Path, required=True)
    cmd.add_argument("--graph", type=Path)
    args = parser.parse_args(argv)
    try:
        run = driver.load_run(args.config, args.graph)
        receipt = run_g2(run)
        path = g0.write_receipt(run, receipt)
    except driver.Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    for row in receipt["rows"]:
        print(f"{row['status']:4} {row['row']} {row['detail']}")
    print(f"G2 {receipt['status']}: {path}")
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
