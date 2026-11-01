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

    python tools/release_driver.py plan   --config run.json [--offline [--ids ids.json] [--recorded cmds.json]]
    python tools/release_driver.py run    --config run.json [--only STAGE | --through STAGE] [--replace]
    python tools/release_driver.py check  --config run.json [--only STAGE | --through STAGE]
    python tools/release_driver.py gate-commit --config run.json --confirm-release-id ID
    python tools/release_driver.py status --config run.json
    python tools/release_driver.py stop   --config run.json

The graph is validated against ``ops/mvp2-release/release-stages.schema.json`` on load.
``run`` records each stage's PID and start token and kills only those (``stop``, Ctrl-C); a
stage whose process died is resumed from its own boundary, never from the first stage.

Exit 0 on success, 1 when a stage failed, 2 on a refusal or usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any

from quant_engine.core import process_alive, process_started

GRAPH_DEFAULT = (
    Path(__file__).resolve().parents[1] / "ops" / "mvp2-release" / "release-stages.json"
)
SCHEMA_DEFAULT = GRAPH_DEFAULT.with_name("release-stages.schema.json")
STATE_NAME = "release-driver-state.json"
TIMING_NAME = "release-driver-timing.log"
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

    def __init__(
        self,
        config: Mapping[str, Any],
        graph: Mapping[str, Any],
        *,
        offline: bool = False,
    ) -> None:
        self.offline = offline
        self.graph = graph
        self.values: dict[str, str] = {
            key: str(value)
            for key, value in config.items()
            if isinstance(value, (str, int))
        }
        self.config = config
        self.values.setdefault("node", "node")
        for key in ("repo_root", "python", "cycle", "run", "code_commit", "mode"):
            if key not in self.values:
                raise Refusal(f"run config lacks {key!r}")
        if self.values["mode"] not in ("probe", "release"):
            raise Refusal("run config mode must be 'probe' or 'release'")
        self.repo = Path(self.values["repo_root"])
        self.cycle = Path(self.values["cycle"])
        self.state_path = self.cycle / STATE_NAME
        self.state: dict[str, Any] = (
            _read_json(self.state_path)
            if self.state_path.is_file() and not offline
            else {"stages": {}}
        )
        self.derived: dict[str, str] = {}
        self.binding = hashlib.sha256(
            json.dumps([config, graph], sort_keys=True).encode()
        ).hexdigest()
        self.confirmed_release_id: str | None = None

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
        if self.offline:
            return f"<{name}>"
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


def stage_argv(
    run: Run, stage: Mapping[str, Any], items: Sequence[Any] | None = None
) -> list[str]:
    """The stage's command line: placeholders resolved, optional groups kept only when configured."""

    argv: list[str] = []
    for item in stage["argv"] if items is None else items:
        if isinstance(item, str):
            argv.append(run.expand(item))
        elif item["if"] in run.values:
            argv.extend(run.expand(arg) for arg in item["args"])
    return argv


def check_config_paths(run: Run) -> None:
    """Configured paths the graph names must exist and lie inside this cycle.

    A root rewrite that leaves a cache base or run directory pointing at an earlier pass is
    refused here, before any stage reads it.
    """

    for file, sha in sorted(dict(run.config.get("pinned_files", {})).items()):
        if not Path(file).is_file() or _file_sha(Path(file)) != sha:
            raise Refusal(
                f"pinned input {file} is missing or does not match its pinned sha256 (stale evidence?)"
            )
    for key, rule in sorted(run.graph.get("config_paths", {}).items()):
        if key not in run.values:
            continue
        path = Path(run.values[key])
        if rule.get("exists") and not path.exists():
            raise Refusal(f"config {key}={path} does not exist (stale path?)")
        if "under" in rule:
            base = Path(run.expand(rule["under"]))
            if not _inside(path, base):
                raise Refusal(
                    f"config {key}={path} is outside this run's cycle {base} (path left from another pass?)"
                )


def check_script_in_repo(
    run: Run, stage: Mapping[str, Any], argv: Sequence[str]
) -> None:
    """The script a stage runs is a file inside the run's own repository, never a probe or older clone."""

    if "builtin" in stage or len(argv) < 2:
        return
    if Path(argv[0]).name.lower().split(".")[0] not in ("python", "python3", "node"):
        return
    script = Path(argv[1])
    if script.suffix not in (".py", ".mjs", ".js", ".sh"):
        return
    if not script.is_file():
        raise Refusal(f"{stage['id']}: script {script} does not exist")
    if "pinned_script" in stage:
        # an operator-supplied script outside the repository, named by its sha256
        path_template, sha_template = stage["pinned_script"]
        if Path(run.expand(path_template)) == script:
            if _file_sha(script) != run.expand(sha_template):
                raise Refusal(
                    f"{stage['id']}: script {script} does not match its pinned sha256"
                )
            return
    if not _inside(script, run.repo):
        raise Refusal(
            f"{stage['id']}: script {script} is outside the run's repository {run.repo}"
        )


def check_bound_ids(run: Run, stage: Mapping[str, Any]) -> None:
    """A hand-built input file must name the generations this run produced, not another pass's."""

    for file_template, id_template in stage.get("bound_ids", []):
        file, wanted = Path(run.expand(file_template)), run.expand(id_template)
        if not file.is_file():
            raise Refusal(f"{stage['id']}: {file} does not exist")
        if wanted not in file.read_text(encoding="utf-8"):
            raise Refusal(
                f"{stage['id']}: {file.name} does not bind {wanted[:16]} (stale input from another pass?)"
            )


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


def spec_topology(spec_file: Path) -> str:
    """Hash of what a release spec binds, independent of where the roots live.

    A replica spec and the real spec of the same release differ only in paths: node ids,
    dependencies, generation ids and manifest hashes, artifact hashes, companions, code
    commit, as-of date and pipeline id are identical (checked on the R1 specs).
    """

    spec = _read_json(spec_file)
    rows = []
    for node in spec["nodes"]:
        if node["kind"] == "GENERATION":
            rows.append(
                [
                    node["node_id"],
                    node["kind"],
                    node["depends_on"],
                    node["root_id"],
                    node["generation_id"],
                    node["manifest_sha256"],
                    node["release_target"],
                ]
            )
        else:
            rows.append(
                [node["node_id"], node["kind"], node["depends_on"], node["sha256"]]
            )
    rows.sort(key=str)
    payload = {
        "nodes": rows,
        "companions": sorted(
            (
                {
                    key: item[key]
                    for key in ("companion_id", "root_id", "relative_path", "sha256")
                }
                for item in spec["companions"]
            ),
            key=lambda item: item["companion_id"],
        ),
        "code_commit": spec["code_commit"],
        "as_of_date": spec["as_of_date"],
        "pipeline_id": spec["pipeline_id"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _check_replica_agrees(
    run: Run, stage: Mapping[str, Any], gate: Mapping[str, Any], real_spec: Path
) -> None:
    """Optional: the driver's own replica checks (core's replica gate is the authority on the receipt)."""

    replica_spec = Path(run.expand(gate["replica_spec"]))
    if not replica_spec.is_file():
        raise Refusal(
            f"{stage['id']}: no replica spec at {replica_spec}; the replica ceremony has not run"
        )
    for replica_stage in gate.get("replica_stages", []):
        record = run.state["stages"].get(replica_stage)
        if (
            not record
            or record["status"] != "ok"
            or record["code_commit"] != run.values["code_commit"]
        ):
            raise Refusal(
                f"{stage['id']}: replica receipt {replica_stage} is missing, failed, or from another code identity"
            )
    if spec_topology(real_spec) != spec_topology(replica_spec):
        raise Refusal(
            f"{stage['id']}: the replica spec binds a different topology than the real spec"
        )
    for real_input, replica_input in gate.get("inputs_same", []):
        left, right = Path(run.expand(real_input)), Path(run.expand(replica_input))
        if path_digest(left) is None or path_digest(left) != path_digest(right):
            raise Refusal(
                f"{stage['id']}: {left.name} differs between the real run and the replica"
            )


def check_commit_gate(run: Run, stage: Mapping[str, Any]) -> None:
    """``execute --commit`` runs only for the exact release the owner confirmed, from a prepared, unmoved state."""

    gate = stage.get("commit_gate")
    if gate is None:
        return
    if run.values["mode"] != "release":
        raise Refusal(f"{stage['id']}: a commit needs mode 'release'")
    release_id = run.derive("release_id")
    if run.confirmed_release_id != release_id:
        raise Refusal(
            f"{stage['id']}: commit needs --confirm-release-id {release_id}, the exact release the owner confirmed"
        )
    real_spec = Path(run.expand(gate["spec"]))
    if _read_json(real_spec)["code_commit"] != run.values["code_commit"]:
        raise Refusal(f"{stage['id']}: the real spec binds another code commit")
    if (
        "replica_receipt" in gate
        and not Path(run.expand(gate["replica_receipt"])).is_file()
    ):
        raise Refusal(
            f"{stage['id']}: no replica receipt at {run.expand(gate['replica_receipt'])}"
        )
    if "replica_verify" in gate:
        # core's gate is the authority: the receipt names all seven ceremony steps (commit,
        # post-release verify, rehearsal, restore among them) for this spec's topology and code
        done = subprocess.run(
            [run.expand(item) for item in gate["replica_verify"]],
            cwd=run.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise Refusal(
                f"{stage['id']}: replica receipt does not verify against the real spec: "
                f"{(done.stderr or done.stdout).strip()[-300:]}"
            )
    if "replica_spec" in gate:
        _check_replica_agrees(run, stage, gate, real_spec)
    prepared = (
        Path(_read_json(real_spec)["coordination_root"]) / "releases" / release_id
    )
    if not prepared.is_dir():
        raise Refusal(
            f"{stage['id']}: release {release_id[:12]} is not prepared at {prepared}"
        )
    recorded = _read_json(Path(run.expand(gate["pointer_snapshot"])))
    now = pointer_snapshot(real_spec)
    moved = [
        key
        for key in sorted(recorded)
        if key != "release_dirs" and recorded[key] != now.get(key)
    ]
    if moved:
        raise Refusal(f"{stage['id']}: pointers moved since the dry run: {moved[:4]}")


def _claimed_outputs(run: Run, stage: Mapping[str, Any]) -> list[Path]:
    return _outputs(run, stage) + [
        Path(run.expand(item)) for item in stage.get("must_not_exist", [])
    ]


def check_ready(
    run: Run,
    stage: Mapping[str, Any],
    *,
    replace: bool,
    resuming: bool = False,
) -> None:
    """Every refusal that applies before ``stage`` runs. Raises ``Refusal``; changes nothing."""

    run.require_frozen_repo()
    check_config_paths(run)
    check_no_live_stage(run)
    if "argv" in stage:
        check_script_in_repo(run, stage, stage_argv(run, stage))
    check_probe_guard(run, stage)
    check_commit_gate(run, stage)
    if "only_mode" in stage and run.values["mode"] != stage["only_mode"]:
        raise Refusal(f"{stage['id']} runs only in {stage['only_mode']} mode")
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
    for path in _claimed_outputs(run, stage):
        if path.exists() and not replace and not resuming:
            where = "an earlier attempt of this run" if own else "an earlier pass"
            raise Refusal(
                f"{stage['id']} would collide with {path}, left by {where}; rerun with --replace"
            )
    check_bound_ids(run, stage)
    for item in stage.get("updates", []):
        if not Path(run.expand(item)).exists():
            raise Refusal(
                f"{stage['id']}: {run.expand(item)} does not exist to continue"
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
        if "exists" in check:
            target = Path(run.expand(check["exists"]))
            if not target.exists():
                failures.append(f"{target} does not exist")
            continue
        file = Path(run.expand(check["json"]))
        try:
            actual = _json_path(_read_json(file), check["path"])
        except (OSError, KeyError, TypeError, ValueError) as error:
            failures.append(f"{file}: {check['path']} unreadable ({error})")
            continue
        if "one_of" in check:
            if actual not in check["one_of"]:
                failures.append(
                    f"{file}: {check['path']} is {actual!r}, expected one of {check['one_of']!r}"
                )
        elif actual != check["equals"]:
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


def replica_provider_generations(run: Run, target: Path) -> dict[str, Any]:
    """The replica's provider-generations list: the real list with every real-workspace source remapped."""

    source = Path(run.values["source_workspace"])
    replica = Path(run.values["replica_workspace"])
    real_rows = _read_json(Path(run.expand("{cycle}/provider-generations.json")))
    rows, remapped = [], 0
    for row in real_rows:
        directory = Path(row["source_dir"])
        if _inside(directory, source):
            directory = replica / directory.resolve().relative_to(source.resolve())
            remapped += 1
        if _inside(directory, source):
            raise Refusal(
                f"replica-provider-generations: {row['root_id']} still names the real workspace"
            )
        if not directory.is_dir():
            raise Refusal(
                f"replica-provider-generations: {row['root_id']} source {directory} does not exist"
            )
        rows.append({"root_id": row["root_id"], "source_dir": str(directory)})
    _write_json(target, rows)
    return {"roots": len(rows), "remapped": remapped}


def _no_builtin_work(run: Run, target: Path) -> dict[str, Any]:
    return {}


_BUILTINS: dict[str, Callable[[Run, Path], dict[str, Any]]] = {
    "inputs_only": _no_builtin_work,
    "provider_generations": provider_generations,
    "replica_provider_generations": replica_provider_generations,
}


def check_no_live_stage(run: Run) -> None:
    """Refuse while a stage this run launched is still alive (the PID and start token it recorded)."""

    for stage_id, record in sorted(run.state["stages"].items()):
        if record.get("status") == "running" and process_alive(
            int(record.get("pid", 0)), record.get("process_started")
        ):
            raise Refusal(
                f"stage {stage_id} is still running as pid {record['pid']}; wait for it or use `stop`"
            )


def reconcile_state(run: Run) -> list[str]:
    """A stage recorded ``running`` whose process is gone was interrupted: say so, keep its attempt."""

    marked = []
    for stage_id, record in sorted(run.state["stages"].items()):
        if record.get("status") == "running" and not process_alive(
            int(record.get("pid", 0)), record.get("process_started")
        ):
            record["status"] = "interrupted"
            record["failures"] = [
                f"process {record.get('pid')} ended without recording an outcome"
            ]
            marked.append(stage_id)
    if marked:
        run.save()
    return marked


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    kill_group = getattr(os, "killpg")
    try:
        kill_group(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while process_alive(pid, None) and time.monotonic() < deadline:
        time.sleep(0.2)
    if process_alive(pid, None):
        try:
            kill_group(pid, getattr(signal, "SIGKILL"))
        except ProcessLookupError:
            pass


def stop_own(run: Run) -> list[str]:
    """Kill only the process trees of stages this driver recorded, and only if the start token still matches."""

    stopped = []
    for stage_id, record in sorted(run.state["stages"].items()):
        pid, token = int(record.get("pid", 0)), record.get("process_started")
        if record.get("status") != "running" or not pid:
            continue
        if token and process_alive(pid, token):
            _kill_tree(pid)
            stopped.append(f"{stage_id} pid {pid}")
        record["status"] = "interrupted"
        record["failures"] = ["stopped by the driver"]
    run.save()
    return stopped


def _timing(run: Run, text: str) -> None:
    run.cycle.mkdir(parents=True, exist_ok=True)
    with (run.cycle / TIMING_NAME).open("a", encoding="utf-8") as handle:
        handle.write(f"{_now()} {text}\n")


def _launch(
    run: Run,
    stage_id: str,
    record: dict[str, Any],
    argv: Sequence[str],
    stdout: Any,
    stderr: Any,
    env: Mapping[str, str],
) -> int:
    """Start the stage's process, record its PID and start token, wait for it. Kills only its own child."""

    child = subprocess.Popen(
        argv,
        cwd=run.repo,
        stdout=stdout,
        stderr=stderr,
        env=dict(env),
        start_new_session=os.name != "nt",
    )
    record["pid"] = child.pid
    record["process_started"] = process_started(child.pid)
    run.save()
    try:
        return child.wait()
    except BaseException:
        if child.poll() is None:
            _kill_tree(child.pid)
        record["status"] = "interrupted"
        run.save()
        raise


def _counters(run: Run, stage: Mapping[str, Any], *scan: Path) -> dict[str, Any] | None:
    if "counters" in stage:
        counters = Path(run.expand(stage["counters"]))
        if counters.is_file():
            found = _read_json(counters).get("work_counters")
            return found if isinstance(found, dict) else None
    keys = stage.get("counter_keys", [])
    if not keys:
        return None
    out: dict[str, Any] = {}
    for path in scan:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in keys:
            hits = re.findall(rf"(?<!\w){re.escape(key)}[\"']?\s*[:=]\s*(\d+)", text)
            if hits:
                out[key] = int(hits[-1])
    return out or None


def execute_stage(
    run: Run,
    stage: Mapping[str, Any],
    *,
    replace: bool,
    resuming: bool = False,
) -> dict[str, Any]:
    check_ready(run, stage, replace=replace, resuming=resuming)
    outputs = _outputs(run, stage)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    previous = run.state["stages"].get(stage["id"], {})
    if not resuming:
        for path in _claimed_outputs(run, stage):
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
    record: dict[str, Any] = {
        "status": "running",
        "binding": run.binding,
        "code_commit": run.values["code_commit"],
        "started_at": started_at,
        "attempt": attempt,
        "resumed": resuming,
        "log": str(log_path),
        "outputs": {},
        "inputs": {},
        "failures": [],
    }
    run.state["stages"][stage["id"]] = record
    run.save()
    _timing(run, f"{stage['id']} start attempt={attempt} resumed={resuming}")
    rc = 0
    stdout_to = Path(run.expand(stage["stdout_to"])) if "stdout_to" in stage else None
    if "builtin" in stage:
        try:
            _BUILTINS[stage["builtin"]](run, outputs[0] if outputs else log_path)
        except Refusal as refusal:
            log_path.write_text(str(refusal), encoding="utf-8")
            record["status"] = "failed"
            record["failures"] = [str(refusal)]
            run.save()
            raise
        log_path.write_text(f"builtin {stage['builtin']}\n", encoding="utf-8")
    else:
        template = stage["resume"].get("argv") if resuming else None
        argv = stage_argv(run, stage, template)
        env = {
            **os.environ,
            **{k: run.expand(v) for k, v in stage.get("env", {}).items()},
        }
        if stdout_to is not None:
            stdout_to.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            if stdout_to is None:
                rc = _launch(run, stage["id"], record, argv, log, log, env)
            else:
                with stdout_to.open("wb") as captured:
                    rc = _launch(run, stage["id"], record, argv, captured, log, env)
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
    record.update(
        {
            "status": "ok" if not failures else "failed",
            "inputs": {
                str(Path(run.expand(item))): path_digest(Path(run.expand(item)))
                for item in stage.get("inputs", [])
            },
            "failures": failures,
            "finished_at": _now(),
            "seconds": elapsed,
            "seconds_total": round(
                float(previous.get("seconds_total", 0.0)) + elapsed, 1
            ),
            "rc": rc,
            "outputs": {str(path): path_digest(path) for path in outputs},
        }
    )
    counters = _counters(run, stage, log_path, *([stdout_to] if stdout_to else []))
    if counters is not None:
        record["work_counters"] = counters
    run.state["stages"][stage["id"]] = record
    run.save()
    _timing(
        run,
        f"{stage['id']} {record['status']} rc={rc} seconds={elapsed}"
        + (f" counters={json.dumps(counters, sort_keys=True)}" if counters else ""),
    )
    return record


def validate_graph(graph: Mapping[str, Any]) -> None:
    """The stage graph conforms to its schema and its stages are ordered and unique."""

    import jsonschema  # type: ignore[import-untyped]

    errors = sorted(
        jsonschema.Draft202012Validator(_read_json(SCHEMA_DEFAULT)).iter_errors(graph),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        shown = "; ".join(
            f"{'/'.join(str(p) for p in error.absolute_path)}: {error.message[:100]}"
            for error in errors[:4]
        )
        raise Refusal(f"stage graph does not match its schema: {shown}")
    seen: list[str] = []
    for stage in graph["stages"]:
        if stage["id"] in seen:
            raise Refusal(f"stage graph repeats stage {stage['id']!r}")
        for required in stage.get("requires", []):
            if required not in seen:
                raise Refusal(
                    f"{stage['id']} requires {required}, which does not precede it"
                )
        seen.append(stage["id"])


def load_run(
    config_path: Path, graph_path: Path | None, *, offline: bool = False
) -> Run:
    config = _read_json(config_path)
    graph = _read_json(graph_path or Path(config.get("stage_graph") or GRAPH_DEFAULT))
    validate_graph(graph)
    run = Run(config, graph, offline=offline)
    return run


def _select(run: Run, only: str | None, through: str | None) -> list[str]:
    ids = run.stage_ids()
    for name in (only, through):
        if name is not None and name not in ids:
            raise Refusal(f"no stage {name!r}; stages are {ids}")
    if only is not None:
        return [only]
    # an explicit stage (the commit) never runs as part of "everything"; name it with --through or --only
    if through is not None:
        needed = {through}
        for i in reversed(ids[: ids.index(through) + 1]):
            if i in needed:
                needed.update(run.stage(i).get("requires", []))
        return [
            i
            for i in ids[: ids.index(through) + 1]
            if i in needed or not run.stage(i).get("explicit")
        ]
    return [i for i in ids if not run.stage(i).get("explicit")]


def check_run(run: Run, selected: Sequence[str]) -> list[str]:
    """Every stale, wrong-generation and leftover refusal, reported without running or changing anything."""

    problems: list[str] = []
    try:
        run.require_frozen_repo()
        check_config_paths(run)
        check_no_live_stage(run)
    except Refusal as refusal:
        return [str(refusal)]
    first_open: str | None = None
    for stage_id in selected:
        stage, record = run.stage(stage_id), run.state["stages"].get(stage_id)
        if record and record["status"] == "ok":
            try:
                verify_kept(run, stage, record)
            except Refusal as refusal:
                problems.append(str(refusal))
            continue
        if first_open is None:
            first_open = stage_id
            try:
                check_ready(
                    run,
                    stage,
                    replace=bool(record),
                    resuming=bool(record and "resume" in stage),
                )
            except Refusal as refusal:
                problems.append(f"{stage_id}: {refusal}")
            continue
        if record is None:
            try:
                leftovers = [p for p in _claimed_outputs(run, stage) if p.exists()]
            except Refusal:
                continue
            problems += [
                f"{stage_id}: {path} is left over from an earlier pass"
                for path in leftovers
            ]
    return problems


def _norm(token: str) -> str:
    text = token.replace("\\", "/").lower()
    return re.sub(r"^/([a-z])/", r"\1:/", text)


def _flag_groups(argv: Sequence[str]) -> tuple[list[str], dict[str, list[str]]]:
    lead: list[str] = []
    flags: dict[str, list[str]] = {}
    current: list[str] | None = None
    for token in argv:
        if token.startswith("--"):
            current = flags.setdefault(token, [])
        elif current is None:
            lead.append(_norm(token))
        else:
            current.append(_norm(token))
    return lead, flags


def compare_command(planned: Sequence[str], recorded: Sequence[str]) -> list[str]:
    """Differences between a planned and a recorded command line, flag by flag (order-insensitive)."""

    plead, pflags = _flag_groups(planned)
    rlead, rflags = _flag_groups(recorded)
    out = []
    # the script is compared by name: where it lives is the run repository's business
    # (``check_script_in_repo`` refuses a script outside it), not part of the command line
    if len(plead) > 1 and len(rlead) > 1:
        plead[1], rlead[1] = Path(plead[1]).name, Path(rlead[1]).name
    if plead != rlead:
        out.append(f"command differs: planned {plead} recorded {rlead}")
    for flag in sorted(set(pflags) | set(rflags)):
        if flag not in rflags:
            out.append(f"{flag}: only in the plan ({' '.join(pflags[flag])})")
        elif flag not in pflags:
            out.append(f"{flag}: only in the recorded run ({' '.join(rflags[flag])})")
        elif pflags[flag] != rflags[flag]:
            out.append(
                f"{flag}: planned {' '.join(pflags[flag])} recorded {' '.join(rflags[flag])}"
            )
    return out


def offline_plan(
    run: Run, selected: Sequence[str], recorded: Mapping[str, Any] | None
) -> tuple[list[str], list[str]]:
    """The command line of every selected stage from the config and graph alone; no disk is read for ids.

    Returns the printed plan and the unexplained differences from the recorded command lines
    (``recorded['stages'][id]['argv']``; a difference listed in ``known`` with a reason is allowed).
    """

    lines: list[str] = []
    problems: list[str] = []
    rows = (recorded or {}).get("stages", {})
    for stage_id in selected:
        stage = run.stage(stage_id)
        if "builtin" in stage:
            lines.append(f"{stage_id}: builtin {stage['builtin']}")
            continue
        argv = stage_argv(run, stage)
        lines.append(f"{stage_id}: {' '.join(argv)}")
        if stage_id not in rows:
            continue
        known = {item["flag"] for item in rows[stage_id].get("known", [])}
        for difference in compare_command(argv, rows[stage_id]["argv"]):
            flag = difference.split(":")[0]
            if flag not in known:
                problems.append(f"{stage_id}: {difference}")
    return lines, problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=("plan", "run", "status", "check", "gate-commit", "stop"),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--only")
    parser.add_argument("--through")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="move colliding create-only outputs aside",
    )
    parser.add_argument(
        "--confirm-release-id",
        help="the exact release id the owner confirmed (commit stage)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="plan: print each stage's command line from the config alone (reads no run state)",
    )
    parser.add_argument(
        "--recorded",
        type=Path,
        help="plan --offline: compare with recorded command lines",
    )
    parser.add_argument(
        "--ids",
        type=Path,
        help="plan --offline: derived ids (a JSON object name -> value)",
    )
    args = parser.parse_args(argv)
    try:
        run = load_run(args.config, args.graph, offline=args.offline)
        run.confirmed_release_id = args.confirm_release_id
        selected = _select(run, args.only, args.through)
        if args.command == "plan":
            if not args.offline:
                for stage_id in selected:
                    print(stage_id)
                return 0
            if args.ids is not None:
                run.derived.update(_read_json(args.ids))
            recorded = _read_json(args.recorded) if args.recorded else None
            lines, problems = offline_plan(run, selected, recorded)
            print("\n".join(lines))
            for problem in problems:
                print(f"differs: {problem}", file=sys.stderr)
            return 2 if problems else 0
        if args.command == "status":
            for stage_id in run.stage_ids():
                record = run.state["stages"].get(stage_id)
                print(
                    stage_id,
                    record["status"] if record else "pending",
                    record["seconds"] if record and "seconds" in record else "",
                    record.get("work_counters", "") if record else "",
                )
            return 0
        if args.command == "check":
            problems = check_run(run, selected)
            for problem in problems:
                print(f"refused: {problem}", file=sys.stderr)
            print("check: ok" if not problems else f"check: {len(problems)} problem(s)")
            return 2 if problems else 0
        if args.command == "gate-commit":
            gated = [row["id"] for row in run.graph["stages"] if "commit_gate" in row]
            for stage_id in gated:
                run.require_frozen_repo()
                check_commit_gate(run, run.stage(stage_id))
            print(f"gate-commit: open for {gated}")
            return 0
        if args.command == "stop":
            for line in stop_own(run) or ["nothing of this run is running"]:
                print(line)
            return 0
        for stage_id in reconcile_state(run):
            print(f"{stage_id}: interrupted earlier; will resume from its own boundary")
        for stage_id in selected:
            stage = run.stage(stage_id)
            record = run.state["stages"].get(stage_id)
            if record and record["status"] == "ok" and args.only is None:
                verify_kept(run, stage, record)
                print(f"{stage_id}: ok (kept)")
                continue
            resuming = bool(
                record
                and record["status"] in ("failed", "interrupted")
                and "resume" in stage
                and not args.replace
            )
            print(
                f"{stage_id}: {'resume' if resuming else 'start'} {_now()}", flush=True
            )
            result = execute_stage(
                run,
                stage,
                replace=args.replace or (bool(record) and not resuming),
                resuming=resuming,
            )
            print(f"{stage_id}: {result['status']} in {result['seconds']}s")
            if result["status"] != "ok":
                for failure in result["failures"]:
                    print(f"  {failure}", file=sys.stderr)
                print(f"  log: {result['log']}", file=sys.stderr)
                return 1
        return 0
    except KeyboardInterrupt:
        print(
            "interrupted: the stage's process tree was stopped; rerun to resume",
            file=sys.stderr,
        )
        return 130
    except Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
