"""Repo-owned release driver (RELEASE-DRIVER-001).

Runs the release stages declared in a stage graph (``ops/mvp2-release/release-stages.json``)
in order, with the same tools and flags the hand-written R1/R2 chain scripts used, but
with every id and path derived from the run's own outputs instead of substituted into
a script by string replacement. The graph is data: this module holds no tool names.

A stage may only start when

* the repository is the run's frozen code identity (``code_commit``, clean tree);
* every stage it requires finished under that identity and its recorded outputs still
  hash to what was recorded (so a leftover from another pass is never read as input);
* none of its create-only outputs exists from an earlier pass (``--replace`` moves them
  aside instead of deleting them);
* every ``assert_equal`` pair of derived ids agrees (a stage never runs on a
  generation other than the one the run produced).

A stage fails on a non-zero exit code, a failed ``check`` or a moved repository; the
exit code of the tool is read directly, never inferred from a log.

Usage::

    python tools/release_driver.py plan   --config run.json
    python tools/release_driver.py run    --config run.json [--only STAGE | --through STAGE] [--replace]
    python tools/release_driver.py status --config run.json

Exit 0 on success, 1 when a stage failed, 2 on a refusal or usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

GRAPH_DEFAULT = (
    Path(__file__).resolve().parents[1] / "ops" / "mvp2-release" / "release-stages.json"
)
STATE_NAME = "release-driver-state.json"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Refusal(Exception):
    """The driver will not run a stage: the run's inputs or state are not what the stage needs."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")
    )


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def path_digest(path: Path) -> str | None:
    """Content hash of a file; for a directory, the hash of every file's relative path and content."""

    if path.is_file():
        return _file_sha(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for item in sorted(path.rglob("*")):
            if item.is_file():
                name = item.relative_to(path).as_posix()
                digest.update(f"{name}\x00{_file_sha(item)}\n".encode())
        return digest.hexdigest()
    return None


class Run:
    """One release run: configuration, derived ids, state and the repository identity."""

    def __init__(self, config: Mapping[str, Any], graph: Mapping[str, Any]) -> None:
        self.graph = graph
        self.values: dict[str, str] = {
            key: str(value)
            for key, value in config.items()
            if isinstance(value, (str, int))
        }
        self.config = config
        for key in ("repo_root", "python", "cycle", "run", "code_commit", "mode"):
            if key not in self.values:
                raise Refusal(f"run config lacks {key!r}")
        if self.values["mode"] not in ("probe", "release"):
            raise Refusal("run config mode must be 'probe' or 'release'")
        self.repo = Path(self.values["repo_root"])
        self.cycle = Path(self.values["cycle"])
        self.state_path = self.cycle / STATE_NAME
        self.state: dict[str, Any] = (
            _read_json(self.state_path) if self.state_path.is_file() else {"stages": {}}
        )
        self.derived: dict[str, str] = {}
        self.binding = hashlib.sha256(
            json.dumps([config, graph], sort_keys=True).encode()
        ).hexdigest()

    # -- templates -------------------------------------------------------------------
    def expand(self, template: str) -> str:
        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in self.values:
                return self.values[name]
            if name in self.graph.get("paths", {}):
                self.values[name] = self.expand(self.graph["paths"][name])
                return self.values[name]
            if name in self.graph.get("derive", {}):
                return self.derive(name)
            raise Refusal(f"unknown placeholder {{{name}}} in {template!r}")

        return _PLACEHOLDER.sub(sub, template)

    def derive(self, name: str) -> str:
        if name in self.derived:
            return self.derived[name]
        rule = self.graph["derive"][name]
        if "single_dir" in rule:
            root = Path(self.expand(rule["single_dir"]))
            names = (
                sorted(item.name for item in root.iterdir()) if root.is_dir() else []
            )
            if len(names) != 1:
                raise Refusal(
                    f"{name}: expected exactly one entry under {root}, found {names}"
                )
            value = names[0]
        elif "json_field" in rule:
            file_template, key = rule["json_field"]
            file = Path(self.expand(file_template))
            if not file.is_file():
                raise Refusal(f"{name}: {file} does not exist")
            found = _read_json(file)
            for part in str(key).split("."):
                if not isinstance(found, dict) or part not in found:
                    raise Refusal(f"{name}: {file} has no {key!r}")
                found = found[part]
            value = str(found)
        elif "spec_root" in rule:
            file_template, root_id = rule["spec_root"]
            file = Path(self.expand(file_template))
            if not file.is_file():
                raise Refusal(f"{name}: {file} does not exist")
            roots = _read_json(file).get("roots", {})
            if root_id not in roots:
                raise Refusal(f"{name}: spec {file} has no root {root_id!r}")
            value = str(roots[root_id])
        elif "spec_node_field" in rule:
            file_template, root_id, key = rule["spec_node_field"]
            file = Path(self.expand(file_template))
            if not file.is_file():
                raise Refusal(f"{name}: {file} does not exist")
            nodes = [
                node
                for node in _read_json(file).get("nodes", [])
                if node.get("kind") == "GENERATION" and node.get("root_id") == root_id
            ]
            if len(nodes) != 1:
                raise Refusal(
                    f"{name}: spec {file} has {len(nodes)} generation nodes for {root_id!r}"
                )
            value = str(nodes[0][key])
        else:
            raise Refusal(f"{name}: unknown derive rule {sorted(rule)}")
        self.derived[name] = value
        return value

    # -- repository identity -----------------------------------------------------------
    def _git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            raise Refusal(f"git {' '.join(args)} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    def repo_identity(self) -> dict[str, Any]:
        return {
            "head": self._git("rev-parse", "HEAD"),
            "dirty": len(self._git("status", "--porcelain").splitlines()),
        }

    def require_frozen_repo(self) -> None:
        identity = self.repo_identity()
        if identity["head"] != self.values["code_commit"] or identity["dirty"]:
            raise Refusal(
                f"repository is {identity['head'][:12]} dirty={identity['dirty']}, "
                f"the run is frozen at {self.values['code_commit'][:12]} with a clean tree"
            )

    # -- state --------------------------------------------------------------------------
    def save(self) -> None:
        _write_json(self.state_path, self.state)

    def stage(self, stage_id: str) -> Mapping[str, Any]:
        for row in self.graph["stages"]:
            if row["id"] == stage_id:
                return row  # type: ignore[no-any-return]
        raise Refusal(f"no stage {stage_id!r}")

    def stage_ids(self) -> list[str]:
        return [row["id"] for row in self.graph["stages"]]


def _outputs(run: Run, stage: Mapping[str, Any]) -> list[Path]:
    templates = list(stage.get("outputs", []))
    if "stdout_to" in stage and stage["stdout_to"] not in templates:
        templates.append(stage["stdout_to"])
    return [Path(run.expand(item)) for item in templates]


def _inside(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or base in resolved.parents


def check_probe_guard(run: Run, stage: Mapping[str, Any]) -> None:
    """A probe never reads or writes the live workspace: it runs on a scratch copy of the real roots."""

    if run.values["mode"] != "probe":
        return
    keys, spec_template = stage.get("live_guard_keys", []), stage.get("guard_spec")
    if not keys and spec_template is None:
        return
    if "live_workspace" not in run.values:
        raise Refusal(
            "probe mode needs live_workspace so it can refuse a spec bound to the real roots"
        )
    live = Path(run.values["live_workspace"])
    offenders = [
        f"{key}={run.values[key]}"
        for key in keys
        if _inside(Path(run.values[key]), live)
    ]
    if spec_template is not None:
        spec = _read_json(Path(run.expand(spec_template)))
        bound = [
            ("coordination_root", spec["coordination_root"]),
            ("state_root", spec.get("state_root", "")),
        ]
        bound += list(spec["roots"].items())
        offenders += [
            f"{name}={value}"
            for name, value in bound
            if value and _inside(Path(value), live)
        ]
    if offenders:
        raise Refusal(
            f"{stage['id']}: probe mode refuses paths inside the live workspace {live}: {offenders[:4]}"
        )


def check_ready(run: Run, stage: Mapping[str, Any], *, replace: bool) -> None:
    """Every refusal that applies before ``stage`` runs. Raises ``Refusal``; changes nothing."""

    run.require_frozen_repo()
    check_probe_guard(run, stage)
    state = run.state["stages"]
    for required in stage.get("requires", []):
        record = state.get(required)
        if not record or record.get("status") != "ok":
            raise Refusal(f"{stage['id']} requires {required}, which has not completed")
        if record["code_commit"] != run.values["code_commit"]:
            raise Refusal(
                f"{required} ran at {record['code_commit'][:12]}, not the run's code identity"
            )
        for path, recorded in record["outputs"].items():
            if path_digest(Path(path)) != recorded:
                raise Refusal(
                    f"{required} output {path} changed since it was produced (stale or edited input)"
                )
    own = state.get(stage["id"])
    for path in _outputs(run, stage):
        if path.exists() and not replace:
            where = "an earlier attempt of this run" if own else "an earlier pass"
            raise Refusal(
                f"{stage['id']} would collide with {path}, left by {where}; rerun with --replace"
            )
    for left_template, right_template in stage.get("assert_equal", []):
        left, right = run.expand(left_template), run.expand(right_template)
        if left != right:
            raise Refusal(
                f"{stage['id']}: {left_template} = {left[:16]} but {right_template} = {right[:16]}"
            )
    for item in stage.get("inputs", []):
        if not Path(run.expand(item)).exists():
            raise Refusal(f"{stage['id']}: input {run.expand(item)} does not exist")


def _json_path(document: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        document = document[part]
    return document


def run_checks(run: Run, stage: Mapping[str, Any]) -> list[str]:
    failures = []
    for check in stage.get("checks", []):
        file = Path(run.expand(check["json"]))
        try:
            actual = _json_path(_read_json(file), check["path"])
        except (OSError, KeyError, TypeError, ValueError) as error:
            failures.append(f"{file}: {check['path']} unreadable ({error})")
            continue
        if actual != check["equals"]:
            failures.append(
                f"{file}: {check['path']} is {actual!r}, expected {check['equals']!r}"
            )
    return failures


def pointer_snapshot(spec_file: Path) -> dict[str, Any]:
    """Digest of every pointer a release may move: CURRENT_RELEASE, each root's CURRENT, each companion."""

    spec = _read_json(spec_file)
    coordination = Path(spec["coordination_root"])
    out: dict[str, Any] = {
        "CURRENT_RELEASE": path_digest(coordination / "CURRENT_RELEASE.json")
    }
    for root_id, root in sorted(spec["roots"].items()):
        out[root_id] = path_digest(Path(root) / "CURRENT.json")
    for companion in spec["companions"]:
        target = Path(spec["roots"][companion["root_id"]]) / companion["relative_path"]
        out["companion:" + companion["companion_id"]] = path_digest(target)
    releases = coordination / "releases"
    out["release_dirs"] = (
        sorted(item.name for item in releases.iterdir()) if releases.is_dir() else []
    )
    return out


def pointers_moved(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    run: Run,
    stage: Mapping[str, Any],
) -> list[str]:
    """Pointers that changed, and release directories other than the one this stage prepared."""

    moved = [
        key
        for key in sorted(before)
        if key != "release_dirs" and before[key] != after.get(key)
    ]
    failures = [
        f"pointer {key} moved during a stage that must not move any" for key in moved
    ]
    added = set(after["release_dirs"]) - set(before["release_dirs"])
    allowed = set()
    stdout = Path(run.expand(stage["stdout_to"])) if "stdout_to" in stage else None
    if stdout is not None and stdout.is_file():
        try:
            allowed = {str(_read_json(stdout)["release_id"])}
        except (ValueError, KeyError, TypeError):
            allowed = set()
    if added - allowed:
        failures.append(
            f"unexpected release directories appeared: {sorted(added - allowed)}"
        )
    return failures


def provider_generations(run: Run, target: Path) -> dict[str, Any]:
    """Write the provider-generations list the production spec binds; refuse an incomplete one."""

    wanted = list(run.graph["provider_roots"])
    sources = dict(run.config.get("provider_sources", {}))
    rows = []
    for root_id in wanted:
        template = sources.get(root_id) or run.graph.get(
            "provider_run_sources", {}
        ).get(root_id)
        if template is None:
            raise Refusal(f"provider-generations: no source for {root_id}")
        directory = Path(run.expand(template))
        if not (directory / "generation.json").is_file() and not directory.is_dir():
            raise Refusal(
                f"provider-generations: {root_id} source {directory} does not exist"
            )
        rows.append({"root_id": root_id, "source_dir": str(directory)})
    extra = sorted(set(sources) - set(wanted))
    if extra:
        raise Refusal(
            f"provider-generations: sources for roots the graph does not bind: {extra}"
        )
    _write_json(target, rows)
    return {"roots": len(rows)}


def verify_kept(run: Run, stage: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """A completed stage is reusable only if the run, the code and its bytes are still what it recorded."""

    run.require_frozen_repo()
    if record.get("binding") != run.binding:
        raise Refusal(
            f"{stage['id']} was produced under a different run configuration or stage graph; rerun it with --only {stage['id']}"
        )
    if record["code_commit"] != run.values["code_commit"]:
        raise Refusal(
            f"{stage['id']} ran at {record['code_commit'][:12]}, not the run's code identity"
        )
    for path, recorded in {**record["outputs"], **record.get("inputs", {})}.items():
        if path_digest(Path(path)) != recorded:
            raise Refusal(
                f"{stage['id']}: {path} changed or vanished since the stage recorded it"
            )


def execute_stage(
    run: Run, stage: Mapping[str, Any], *, replace: bool
) -> dict[str, Any]:
    check_ready(run, stage, replace=replace)
    outputs = _outputs(run, stage)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for path in outputs:
        if path.exists():
            path.rename(path.with_name(f"{path.name}.prev-{stamp}"))
    spec_file = (
        Path(run.expand(stage["pointers_unchanged"]))
        if "pointers_unchanged" in stage
        else None
    )
    before = pointer_snapshot(spec_file) if spec_file is not None else None
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = run.cycle / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    attempt = len(list(log_dir.glob(f"{stage['id']}.*.log"))) + 1
    log_path = log_dir / f"{stage['id']}.{attempt}.log"
    started = time.monotonic()
    started_at = _now()
    rc = 0
    if stage.get("builtin") == "provider_generations":
        try:
            provider_generations(run, outputs[0])
        except Refusal as refusal:
            log_path.write_text(str(refusal), encoding="utf-8")
            raise
    else:
        argv = [run.expand(item) for item in stage["argv"]]
        stdout_to = (
            Path(run.expand(stage["stdout_to"])) if "stdout_to" in stage else None
        )
        if stdout_to is not None:
            stdout_to.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            if stdout_to is None:
                rc = subprocess.run(
                    argv, cwd=run.repo, stdout=log, stderr=log, check=False
                ).returncode
            else:
                with stdout_to.open("wb") as captured:
                    rc = subprocess.run(
                        argv, cwd=run.repo, stdout=captured, stderr=log, check=False
                    ).returncode
    elapsed = round(time.monotonic() - started, 1)
    failures: list[str] = []
    if rc != stage.get("expect_rc", 0):
        failures.append(f"exit code {rc}")
    failures += run_checks(run, stage)
    if before is not None and spec_file is not None:
        failures += pointers_moved(before, pointer_snapshot(spec_file), run, stage)
        if "snapshot_to" in stage and not failures:
            _write_json(
                Path(run.expand(stage["snapshot_to"])), pointer_snapshot(spec_file)
            )
    after = run.repo_identity()
    if after["head"] != run.values["code_commit"] or after["dirty"]:
        failures.append(
            f"repository changed during the stage ({after['head'][:12]} dirty={after['dirty']})"
        )
    for path in outputs:
        if not path.exists():
            failures.append(f"declared output {path} was not produced")
    record: dict[str, Any] = {
        "status": "ok" if not failures else "failed",
        "binding": run.binding,
        "inputs": {
            str(Path(run.expand(item))): path_digest(Path(run.expand(item)))
            for item in stage.get("inputs", [])
        },
        "failures": failures,
        "code_commit": run.values["code_commit"],
        "started_at": started_at,
        "seconds": elapsed,
        "rc": rc,
        "log": str(log_path),
        "outputs": {str(path): path_digest(path) for path in outputs},
    }
    if "counters" in stage:
        counters = Path(run.expand(stage["counters"]))
        if counters.is_file():
            record["work_counters"] = _read_json(counters).get("work_counters")
    run.state["stages"][stage["id"]] = record
    run.save()
    return record


def load_run(config_path: Path, graph_path: Path | None) -> Run:
    config = _read_json(config_path)
    graph = _read_json(graph_path or Path(config.get("stage_graph") or GRAPH_DEFAULT))
    if graph.get("schema_version") != 1:
        raise Refusal("unsupported stage graph schema")
    return Run(config, graph)


def _select(run: Run, only: str | None, through: str | None) -> list[str]:
    ids = run.stage_ids()
    for name in (only, through):
        if name is not None and name not in ids:
            raise Refusal(f"no stage {name!r}; stages are {ids}")
    if only is not None:
        return [only]
    return ids[: ids.index(through) + 1] if through is not None else ids


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("plan", "run", "status"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--only")
    parser.add_argument("--through")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="move colliding create-only outputs aside",
    )
    args = parser.parse_args(argv)
    try:
        run = load_run(args.config, args.graph)
        selected = _select(run, args.only, args.through)
        if args.command == "plan":
            for stage_id in selected:
                print(stage_id)
            return 0
        if args.command == "status":
            for stage_id in run.stage_ids():
                record = run.state["stages"].get(stage_id)
                print(
                    stage_id,
                    record["status"] if record else "pending",
                    record["seconds"] if record else "",
                )
            return 0
        for stage_id in selected:
            record = run.state["stages"].get(stage_id)
            if record and record["status"] == "ok" and args.only is None:
                verify_kept(run, run.stage(stage_id), record)
                print(f"{stage_id}: ok (kept)")
                continue
            print(f"{stage_id}: start {_now()}", flush=True)
            result = execute_stage(
                run, run.stage(stage_id), replace=args.replace or bool(record)
            )
            print(f"{stage_id}: {result['status']} in {result['seconds']}s")
            if result["status"] != "ok":
                for failure in result["failures"]:
                    print(f"  {failure}", file=sys.stderr)
                print(f"  log: {result['log']}", file=sys.stderr)
                return 1
        return 0
    except Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
