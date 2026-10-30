"""Four-family release validation, started the moment the candidate is PREPARED.

``run`` waits for ``prepared.json`` (a slow poll of one file, never a process scan), then
builds the masked packet, creates fresh seat directories and launches every command seat
in parallel. ``build_packet`` and ``make_seats`` are importable for the same steps alone.

Consent in force: the rendered report and panel may go to the four QC families, never
raw provider ledgers or credentials. Account ids and UUIDs are masked in the packet; a
context file whose path looks like a ledger is refused, and the run fails if an unmasked
account id survives in any packet file.

Seats are command templates (``--seats-config`` JSON, name -> {"argv": [...]}) with
{seat}, {prompt} and {out} placeholders. A seat with ``"mode": "manual"`` only gets its
directory and prompt; the orchestrator runs it (for example an in-process reviewer).
Default seats: Kimi K3 and GLM 5.3 through C:/Dev/bin/kimi.cmd, Codex through
``codex exec``. Run ``aws sso login --profile bva-aws-bedrock`` yourself before any
Bedrock-hosted seat.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

ACCOUNT = re.compile(r"account-([0-9a-f]{16})")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LEDGER_HINT = re.compile(r"ledger", re.IGNORECASE)
SERIES_LIMIT = 60

PANEL_SECTIONS = (
    "strategies performance performance_tab custody_current_capital capital_reconciliation "
    "portfolio_twr_cumulative portfolio_mwr_annualised hwm_summary allocations observation_windows "
    "account_summary reconciliation_checks attribution_dispositions limitations gates blocking_items "
    "canonical_strategy_series advisory counts capital provenance fact_status strategy_underwriting "
    "meta hwm_governed_proof invested_capital"
).split()
REPORT_SECTIONS = (
    "strategy_summary twr_mwr retained_strategies allocation_history source_clock flow_reconciliation "
    "corrections methodology strategy_attribution portfolio_summary provider_admissions "
    "strategy_provider_evidence coverage_data_quality account_summary provider_coverage"
).split()
FULL_SECTIONS = frozenset(
    {
        "strategies",
        "performance",
        "performance_tab",
        "custody_current_capital",
        "capital_reconciliation",
        "strategy_summary",
        "twr_mwr",
        "retained_strategies",
        "meta",
    }
)

DEFAULT_SEATS: dict[str, dict[str, Any]] = {
    "kimi": {
        "argv": ["C:/Dev/bin/kimi.cmd", "-m", "fireworks/kimi-k3", "-p", "{prompt}"]
    },
    "glm": {
        "argv": ["C:/Dev/bin/kimi.cmd", "-m", "fireworks/glm-5p3", "-p", "{prompt}"]
    },
    "astra": {
        "argv": [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "workspace-write",
            "-C",
            "{seat}",
            "--add-dir",
            "{seat}",
            "-m",
            "gpt-6-astra",
            "-o",
            "{seat}/last.txt",
            "{prompt}",
        ]
    },
    "opus": {"mode": "manual"},
}


class ValidationError(RuntimeError):
    """A named refusal of this tool."""


def mask(text: str) -> str:
    text = ACCOUNT.sub(lambda m: "acct-" + m.group(1)[8:], text)
    return UUID.sub("<uuid>", text)


def shrink(value: Any, depth: int = 0) -> Any:
    """Long series become {_series_len, _first, _last}: the ends stay checkable."""

    if isinstance(value, list):
        if len(value) > SERIES_LIMIT and depth > 0:
            return {
                "_series_len": len(value),
                "_first": [shrink(x, depth + 1) for x in value[:3]],
                "_last": [shrink(x, depth + 1) for x in value[-3:]],
            }
        return [shrink(x, depth + 1) for x in value]
    if isinstance(value, dict):
        return {k: shrink(v, depth + 1) for k, v in value.items()}
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"unreadable json: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"json root is not an object: {path}")
    return value


def _current_generation(run_root: Path, stage: str) -> Path:
    stage_dir = run_root / "stages" / stage
    gid = _load(stage_dir / "CURRENT.json").get("generation_id")
    if not isinstance(gid, str):
        raise ValidationError(f"{stage} CURRENT.json names no generation")
    path = stage_dir / "generations" / gid
    if not path.is_dir():
        raise ValidationError(f"{stage} generation directory is missing: {path}")
    return path


def build_packet(
    *,
    run_root: Path,
    out: Path,
    release_id: str,
    code_commit: str,
    cutoff: str,
    checklist: Path,
    findings_details: Path | None = None,
    context: Sequence[Path] = (),
) -> dict[str, Any]:
    for item in context:
        if LEDGER_HINT.search(str(item)) or item.suffix not in {".md", ".json", ".txt"}:
            raise ValidationError(
                f"context file refused (ledger-like path or type): {item}"
            )
    if out.exists() and any(out.iterdir()):
        raise ValidationError(f"packet directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    panel_gen = _current_generation(run_root, "panel")
    report_gen = _current_generation(run_root, "report")
    panel = _load(panel_gen / "data" / "dashboard_data.json")
    report = _load(report_gen / "portfolio-report-data.json")

    def dump(name: str, obj: Any, full: bool) -> None:
        body = obj if full else shrink(obj)
        (out / name).write_text(
            mask(json.dumps(body, indent=1, sort_keys=True)), encoding="utf-8"
        )

    for key in PANEL_SECTIONS:
        if key in panel:
            dump(f"panel_{key}.json", panel[key], key in FULL_SECTIONS)
    for key in REPORT_SECTIONS:
        if key in report:
            dump(f"report_{key}.json", report[key], key in FULL_SECTIONS)
    if findings_details is not None:
        dump("panel_findings_details.json", _load(findings_details), True)
    (out / "context").mkdir()
    for item in context:
        (out / "context" / item.name).write_text(
            mask(item.read_text(encoding="utf-8")), encoding="utf-8"
        )
    shutil.copyfile(checklist, out / "CHECKLIST.md")
    note = (
        "# Release candidate review packet\n\n"
        f"Release candidate id (PREPARED, pointers unmoved): {release_id}\n"
        f"Panel generation: {panel_gen.name}\nReport generation: {report_gen.name}\n"
        f"Frozen code SHA: {code_commit}\nFinancial cutoff: {cutoff}\n"
        "Account ids are masked to their last 8 hex characters. The packet holds rendered report/panel "
        "values and context notes only: no raw provider ledgers. A claim that needs them is UNVERIFIED.\n"
    )
    (out / "PACKET.md").write_text(note, encoding="utf-8")
    for file in out.rglob("*"):
        if file.is_file() and ACCOUNT.search(
            file.read_text(encoding="utf-8", errors="replace")
        ):
            raise ValidationError(f"unmasked account id survived in {file}")
    return {
        "files": sum(1 for p in out.rglob("*") if p.is_file()),
        "panel_generation": panel_gen.name,
        "report_generation": report_gen.name,
    }


def prompt_for(seat_dir: Path, release_id: str) -> str:
    out = (seat_dir / "out" / "review.md").as_posix()
    return (
        "You are an independent adversarial reviewer of a financial data release candidate. Work alone: "
        "no sub-agents, no network. Read packet/PACKET.md and packet/CHECKLIST.md first (relative to your "
        "working directory), then every other file in packet/ that you need. Follow CHECKLIST.md exactly. "
        "Write your findings ONLY to the output file path given below; the last line of that file must be "
        f"exactly the VERDICT line from CHECKLIST.md with candidate hash {release_id}. Do not write anywhere "
        "else. Account ids are masked; do not try to unmask. Be skeptical: disprove, do not confirm. "
        "Quantify every finding. Never run python through stdin.\n\n"
        f"Your working directory is {seat_dir.as_posix()}. Output file: {out}"
    )


def make_seats(
    packet: Path, seats_root: Path, seats: dict[str, dict[str, Any]], release_id: str
) -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    for name in sorted(seats):
        seat = seats_root / f"seat-{name}"
        if seat.exists() and any(seat.iterdir()):
            raise ValidationError(f"seat directory is not fresh: {seat}")
        (seat / "out").mkdir(parents=True, exist_ok=True)
        shutil.copytree(packet, seat / "packet")
        (seat / "PROMPT.md").write_text(prompt_for(seat, release_id), encoding="utf-8")
        dirs[name] = seat
    return dirs


def _run_seat(
    name: str, spec: dict[str, Any], seat: Path, timeout: int
) -> dict[str, Any]:
    if spec.get("mode") == "manual":
        return {"seat": name, "state": "MANUAL", "prompt": str(seat / "PROMPT.md")}
    prompt = (seat / "PROMPT.md").read_text(encoding="utf-8")
    argv = [
        a.replace("{seat}", seat.as_posix())
        .replace("{prompt}", prompt)
        .replace("{out}", (seat / "out").as_posix())
        for a in spec["argv"]
    ]
    started = time.time()
    try:
        with (seat / "seat.log").open("wb") as log:
            rc = subprocess.run(
                argv,
                cwd=seat,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            ).returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"seat": name, "state": "FAILED", "error": type(exc).__name__}
    return {
        "seat": name,
        "state": "DONE",
        "rc": rc,
        "seconds": round(time.time() - started),
    }


def verdict(seat: Path, release_id: str) -> str:
    review = seat / "out" / "review.md"
    if not review.is_file():
        return "NO_OUTPUT"
    lines = review.read_text(encoding="utf-8").rstrip().splitlines()
    last = lines[-1] if lines else ""
    return (
        last if last.startswith("VERDICT") and release_id in last else "NO_VERDICT_LINE"
    )


def wait_prepared(path: Path, timeout: int, poll: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    while not path.is_file():
        if time.time() > deadline:
            raise ValidationError(f"candidate not PREPARED within {timeout}s: {path}")
        time.sleep(poll)
    prepared = _load(path)
    if set(prepared) != {"release_id", "spec_sha256"}:
        raise ValidationError(
            f"prepared.json has unexpected fields: {sorted(prepared)}"
        )
    return prepared


def run(a: argparse.Namespace) -> dict[str, Any]:
    prepared = wait_prepared(Path(a.prepared_file), a.wait_timeout, a.poll)
    release_id = str(prepared["release_id"])
    seats = dict(DEFAULT_SEATS)
    if a.seats_config:
        seats = json.loads(Path(a.seats_config).read_text(encoding="utf-8"))
    root = Path(a.seats_root)
    packet = root / "packet"
    info = build_packet(
        run_root=Path(a.run_root),
        out=packet,
        release_id=release_id,
        code_commit=a.code_commit,
        cutoff=a.cutoff,
        checklist=Path(a.checklist),
        findings_details=Path(a.findings_details) if a.findings_details else None,
        context=[Path(p) for p in a.context],
    )
    dirs = make_seats(packet, root, seats, release_id)
    with ThreadPoolExecutor(max_workers=len(dirs)) as pool:
        futures = [
            pool.submit(_run_seat, n, seats[n], d, a.seat_timeout)
            for n, d in dirs.items()
        ]
        results = [f.result() for f in futures]
    for row in results:
        manual = row["state"] == "MANUAL"
        row["verdict"] = (
            "PENDING_MANUAL" if manual else verdict(dirs[row["seat"]], release_id)
        )
    summary = {"release_id": release_id, "packet": info, "seats": results}
    (root / "validation-summary.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True), encoding="utf-8"
    )
    return summary


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument(
        "--prepared-file",
        type=Path,
        required=True,
        help="<run root>/prepared.json of the pipeline spec",
    )
    r.add_argument(
        "--run-root",
        type=Path,
        required=True,
        help="attended run output (stages/panel, stages/report)",
    )
    r.add_argument("--seats-root", type=Path, required=True)
    r.add_argument("--checklist", type=Path, required=True)
    r.add_argument("--code-commit", required=True)
    r.add_argument("--cutoff", required=True)
    r.add_argument("--findings-details", type=Path)
    r.add_argument("--context", type=Path, nargs="*", default=[])
    r.add_argument("--seats-config", type=Path)
    r.add_argument("--wait-timeout", type=int, default=6 * 3600)
    r.add_argument("--poll", type=int, default=60)
    r.add_argument("--seat-timeout", type=int, default=3600)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        print(json.dumps(run(a), sort_keys=True))
    except (ValidationError, OSError, ValueError, KeyError) as exc:
        print(f"release-validation: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
