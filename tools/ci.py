"""Run the required CI checks locally, straight from the workflow files.

The workflows stay the single source of truth: this script reads their
single-line ``run:`` steps and executes them, so the local gate cannot drift
from the hosted one. Each job maps to a required status context:

    quality           windows-2025 / Python 3.11
    data-boundary     data-boundary
    secrets           secrets
    dependency-audit  dependency-audit
    workflow-policy   workflow-policy

Usage: ``uv run --frozen --no-sync python tools/ci.py [JOB ...]``. With no job
every one runs. ``--list`` prints the commands without running them,
``--skip-setup`` drops the install/provisioning steps for a warm environment.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
JOBS: Mapping[str, tuple[str, str]] = {
    "quality": ("quality.yml", "quality"),
    "data-boundary": ("data-boundary.yml", "data-boundary"),
    "secrets": ("security.yml", "secrets"),
    "dependency-audit": ("security.yml", "dependency-audit"),
    "workflow-policy": ("security.yml", "workflow-policy"),
}
# Provisioning only: hosted runners need it, a warm local checkout may not.
SETUP = re.compile(r"^(uv sync|npm ci|node apps/.*install chromium|node --input-type)")
# Hosted-runner mechanics with no local meaning (they write GITHUB_* files).
HOSTED_ONLY = re.compile(r"GITHUB_(OUTPUT|PATH)")
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


@dataclass(frozen=True)
class Step:
    name: str
    command: str
    env: Mapping[str, str]


def _expand(expression: str, python: str, base: str, sha: str) -> str:
    known = {
        "matrix.python-version": python,
        "steps.controller_graph_range.outputs.base": base,
        "github.sha": sha,
        "runner.temp": os.environ.get("TEMP", "."),
    }
    if expression not in known:
        raise SystemExit(
            f"tools/ci.py cannot evaluate workflow expression: {expression}"
        )
    return known[expression]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _controller_graph_base() -> str:
    """Local equivalent of the hosted `controller_graph_range` step."""

    for ref in ("origin/main", "main"):
        found = subprocess.run(
            ["git", "merge-base", ref, "HEAD"], cwd=ROOT, capture_output=True, text=True
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    return "EMPTY_TREE"


def plan(job: str, *, skip_setup: bool = False) -> list[Step]:
    """Return the local command list for one required job."""

    workflow, job_name = JOBS[job]
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    spec = document["jobs"][job_name]
    job_env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    matrix = spec.get("strategy", {}).get("matrix", {})
    python = str(
        (matrix.get("python-version") or [job_env.get("UV_PYTHON", "3.11")])[0]
    )
    if job != "quality":
        # Keep `uv sync --package ...` from pruning the main environment.
        job_env["UV_PROJECT_ENVIRONMENT"] = str(
            ROOT / ".codex-bootstrap" / "ci-env" / job
        )
    base = _controller_graph_base()
    sha = _git("rev-parse", "HEAD")
    steps: list[Step] = []
    for step in spec["steps"]:
        run = step.get("run")
        if run is None:
            continue
        text = str(run)
        name = str(step.get("name", text[:40]))
        if HOSTED_ONLY.search(text) or name.startswith("Install checksummed"):
            continue
        env = {
            **job_env,
            **{str(k): str(v) for k, v in (step.get("env") or {}).items()},
        }
        for line in (ln.strip() for ln in text.splitlines() if ln.strip()):
            if skip_setup and SETUP.match(line):
                continue
            expanded = EXPRESSION.sub(
                lambda m: _expand(m.group(1), python, base, sha), line
            )
            resolved = {
                k: EXPRESSION.sub(lambda m: _expand(m.group(1), python, base, sha), v)
                for k, v in env.items()
            }
            steps.append(Step(name, expanded, resolved))
    return steps


def _argv(command: str) -> list[str]:
    argv = shlex.split(command, posix=True)
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    return argv


def run(steps: Sequence[Step]) -> int:
    for step in steps:
        print(f"\n== {step.name}\n$ {step.command[:200]}", flush=True)
        done = subprocess.run(
            _argv(step.command), cwd=ROOT, env={**os.environ, **step.env}
        )
        if done.returncode:
            print(f"FAILED ({done.returncode}): {step.name}", file=sys.stderr)
            return done.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "jobs", nargs="*", help=f"any of {', '.join(JOBS)}; default: all"
    )
    parser.add_argument(
        "--list", action="store_true", help="print commands, run nothing"
    )
    parser.add_argument("--skip-setup", action="store_true", help="skip install steps")
    args = parser.parse_args(argv)
    if unknown := [job for job in args.jobs if job not in JOBS]:
        parser.error(f"unknown job(s): {', '.join(unknown)}")
    for job in args.jobs or list(JOBS):
        steps = plan(job, skip_setup=args.skip_setup)
        print(f"\n##### {job}: {len(steps)} commands")
        if args.list:
            for step in steps:
                print(f"  [{step.name}] {step.command[:160]}")
        elif code := run(steps):
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
