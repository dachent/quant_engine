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
import threading
import time
from typing import Any

from quant_engine.core import (
    ReleaseInputsError,
    ReleaseInputs,
    load_release_inputs,
    process_alive,
    process_started,
)

from quant_engine.core import InterpreterRefusal, require_interpreter
import release_cycle_manifest

GRAPH_DEFAULT = (
    Path(__file__).resolve().parents[1] / "ops" / "mvp2-release" / "release-stages.json"
)
SCHEMA_DEFAULT = GRAPH_DEFAULT.with_name("release-stages.schema.json")
STATE_NAME = "release-driver-state.json"
TIMING_NAME = "release-driver-timing.log"
BASELINE_INPUT = "acceptance/expected-baseline.json"
COMMIT_SCHEME = "git-commit-v1"
TREE_SCHEME = "git-tree-v2"
FULL_CEREMONY = "full"
REPLICA_ONLY = "replica-only"
ATTESTATION_FILENAME = "merge-attestation.json"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Refusal(Exception):
    """The driver will not run a stage: the run's inputs or state are not what the stage needs."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _write_json(path: Path, value: Any) -> None:
    """Write through a sibling temporary file and an atomic replace: a crash leaves the old checkpoint."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(
            (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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
        bind_inputs: bool = True,
    ) -> None:
        self.offline = offline
        self.release_inputs: ReleaseInputs | None = None
        self.graph = graph
        self.values: dict[str, str] = {
            key: str(value)
            for key, value in config.items()
            if isinstance(value, (str, int))
        }
        self.config = config
        self.values.setdefault("node", "node")
        self.values.setdefault("identity_scheme", COMMIT_SCHEME)
        if self.values["identity_scheme"] not in (COMMIT_SCHEME, TREE_SCHEME):
            raise Refusal(
                f"run config identity_scheme must be {COMMIT_SCHEME} or {TREE_SCHEME}"
            )
        if "ceremony" not in self.values:
            raise Refusal(
                f"run config must declare ceremony: {FULL_CEREMONY!r} (real roots are written) "
                f"or {REPLICA_ONLY!r} (nothing outside the replica is written)"
            )
        if self.values["ceremony"] not in (FULL_CEREMONY, REPLICA_ONLY):
            raise Refusal(
                f"run config ceremony must be {FULL_CEREMONY} or {REPLICA_ONLY}"
            )
        if self.values["identity_scheme"] == TREE_SCHEME and not all(
            key in self.values
            for key in ("coordination_root", "candidate_commit", "base_ref")
        ):
            raise Refusal(
                "a git-tree-v2 run needs coordination_root, candidate_commit and base_ref in its config"
            )
        for key in ("repo_root", "python", "cycle", "run", "code_commit", "mode"):
            if key not in self.values:
                raise Refusal(f"run config lacks {key!r}")
        if self.values["mode"] not in ("probe", "release"):
            raise Refusal("run config mode must be 'probe' or 'release'")
        self.repo = Path(self.values["repo_root"])
        self.cycle = Path(self.values["cycle"])
        if bind_inputs:
            self._bind_release_inputs()
        self.state_path = self.cycle / STATE_NAME
        self.state: dict[str, Any] = (
            _read_json(self.state_path)
            if self.state_path.is_file() and not offline
            else {"stages": {}}
        )
        self.derived: dict[str, str] = {}
        # which ceremony runs decides which later stages apply, not what an earlier stage produced
        self._bound_config = {
            key: value for key, value in config.items() if key != "ceremony"
        }
        self._stage_bindings: dict[str, str] = {}
        self.confirmed_release_id: str | None = None
        # stages of one parallel group run in threads of this process: the state file is shared
        self.lock = threading.RLock()
        self.parallel_live: set[str] = set()
        self.git_retries: list[str] = []
        # roots a replica-only run must never write into
        self.real_roots = [
            Path(str(item))
            for item in [
                *config.get("real_roots", []),
                *(
                    self.values[key]
                    for key in (
                        "source_workspace",
                        "live_workspace",
                        "coordination_root",
                    )
                    if key in self.values
                ),
            ]
        ]

    def _bind_release_inputs(self) -> None:
        """Take the expected baseline from a pinned release-inputs root outside Git.

        Only when the run config names ``release_inputs_root``; otherwise the graph's repo
        defaults apply unchanged. Missing, unpinned, tampered or wrong-cutoff inputs refuse.
        """

        root = self.values.get("release_inputs_root")
        if root is None:
            return
        pin = self.values.get("release_inputs_manifest_sha256")
        if pin is None:
            raise Refusal("release_inputs_root needs release_inputs_manifest_sha256")
        try:
            inputs = self.release_inputs = load_release_inputs(root, pin)
            baseline = inputs.path(BASELINE_INPUT)
        except ReleaseInputsError as exc:
            if not self.offline or Path(root).exists():
                raise Refusal(f"release inputs: {exc}") from exc
            # an offline plan may name a root that is not mounted (no such directory; a present but
            # damaged root still refuses): it shows where the baseline would
            # lie and binds no bytes
            shown = str(Path(root) / BASELINE_INPUT)
            self.values["expected_baseline"] = shown
            self.values["expected_baseline_arg"] = shown
            return
        requested = self.values.get("requested_as_of")
        if requested is not None and inputs.cutoff != requested:
            raise Refusal(
                f"release inputs are for cutoff {inputs.cutoff}, the run's is {requested}"
            )
        wanted = self.values.setdefault(
            "expected_baseline_sha256", inputs.sha256(BASELINE_INPUT)
        )
        if wanted != inputs.sha256(BASELINE_INPUT):
            raise Refusal(
                "expected_baseline_sha256 differs from the release inputs' baseline"
            )
        self.values["expected_baseline"] = str(baseline)
        self.values["expected_baseline_arg"] = str(baseline)

    def recheck_release_inputs(self) -> None:
        """Re-hash the baseline the run was bound to: the root may change between stages."""

        if self.release_inputs is None:
            return
        try:
            self.release_inputs.path(BASELINE_INPUT)
        except ReleaseInputsError as exc:
            raise Refusal(f"release inputs: {exc}") from exc

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
        elif "relative_to" in rule:
            path_template, base_template = rule["relative_to"]
            path, base = (
                Path(self.expand(path_template)),
                Path(self.expand(base_template)),
            )
            if not _inside(path, base):
                raise Refusal(f"{name}: {path} is not inside {base}")
            value = path.resolve().relative_to(base.resolve()).as_posix()
        elif "file_sha256" in rule:
            # the digest of a file an earlier stage wrote (a tool that authenticates its input by hash)
            file = Path(self.expand(rule["file_sha256"]))
            if not file.is_file():
                raise Refusal(f"{name}: {file} does not exist")
            value = _file_sha(file)
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

    # -- bindings ----------------------------------------------------------------------
    def interpreter_version(self, python: str) -> tuple[int, ...]:
        """The (major, minor, micro) of ``python``, probed once per run."""

        cache: dict[str, tuple[int, ...]] = self.__dict__.setdefault(
            "_interpreters", {}
        )
        if python not in cache:
            try:
                probe = subprocess.run(
                    [python, "-I", "-c", "import sys; print(*sys.version_info[:3])"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                version = tuple(int(part) for part in probe.stdout.split())
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise Refusal(f"cannot probe the run's Python {python}: {exc}") from exc
            if len(version) != 3:
                raise Refusal(f"cannot read the version of the run's Python {python}")
            cache[python] = version
        return cache[python]

    def stage_binding(self, stage_id: str) -> str:
        """What a stage's result depends on: the run configuration, its own definition, the definitions of
        every stage it requires, and the shared path and derive entries those definitions use.

        A change to a later or unrelated stage (a replica stage, say) leaves this unchanged, so completed
        build stages stay valid.
        """

        if stage_id in self._stage_bindings:
            return self._stage_bindings[stage_id]
        closure: set[str] = set()
        todo = [stage_id]
        while todo:
            current = todo.pop()
            if current not in closure:
                closure.add(current)
                todo.extend(self.stage(current).get("requires", []))
        definitions = [self.stage(item) for item in sorted(closure)]
        names: set[str] = set(_PLACEHOLDER.findall(json.dumps(definitions)))
        shared: dict[str, Any] = {"paths": {}, "derive": {}}
        pending = sorted(names)
        while pending:
            name = pending.pop()
            for table in ("paths", "derive"):
                if name in self.graph.get(table, {}) and name not in shared[table]:
                    shared[table][name] = self.graph[table][name]
                    pending.extend(
                        _PLACEHOLDER.findall(json.dumps(self.graph[table][name]))
                    )
        for key in (
            "provider_roots",
            "provider_run_sources",
            "config_paths",
            "required_python",
        ):
            shared[key] = self.graph.get(key)
        digest = hashlib.sha256(
            json.dumps(
                [self._bound_config, definitions, shared], sort_keys=True
            ).encode()
        ).hexdigest()
        self._stage_bindings[stage_id] = digest
        return digest

    # -- repository identity -----------------------------------------------------------
    def _git(self, *args: str) -> str:
        # an identity read that fails with no message is a transient (R3 refusal 20: another process
        # scanning the tree): read it once more and record that it happened
        for attempt in (1, 2):
            done = subprocess.run(
                ["git", *args],
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode == 0:
                return done.stdout.strip()
            if (
                done.stderr.strip()
                or attempt == 2
                or args[0] not in ("rev-parse", "status")
            ):
                break
            self.git_retries.append(" ".join(args))
        raise Refusal(f"git {' '.join(args)} failed: {done.stderr.strip()}")

    @property
    def scheme(self) -> str:
        return self.values["identity_scheme"]

    def repo_identity(self) -> dict[str, Any]:
        # the identity value under the run's scheme: the commit (v1) or the tree (v2)
        revision = "HEAD^{tree}" if self.scheme == TREE_SCHEME else "HEAD"
        return {
            "head": self._git("rev-parse", revision),
            "commit": self._git("rev-parse", "HEAD"),
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
        with self.lock:
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
        elif "for_each" in item:
            # one group of arguments per entry of a run-configuration map (or list: key = value = the item), in
            # key order; the entry's key and value stand for {each_key} and {each_value}
            # (a repeated ``--root ID=DIR``)
            table = run.config.get(item["for_each"])
            if table is None:
                continue
            if isinstance(table, list):
                table = {str(entry): str(entry) for entry in table}
            if not isinstance(table, dict):
                raise Refusal(
                    f"run config {item['for_each']!r} must be an object of key to value or a list"
                )
            for key in sorted(table):
                argv.extend(
                    run.expand(
                        arg.replace("{each_key}", str(key)).replace(
                            "{each_value}", str(table[key])
                        )
                    )
                    for arg in item["args"]
                )
        elif (
            item["if"] in run.values
            and ("equals" not in item or run.values[item["if"]] == item["equals"])
            and item.get("unless") not in run.values
            and ("if_file" not in item or Path(run.expand(item["if_file"])).is_file())
        ):
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


def mode_line(run: Run) -> str:
    if run.values["ceremony"] == REPLICA_ONLY:
        return (
            "ceremony: replica-only (no write outside the replica; real roots refused)"
        )
    return "ceremony: full (real roots may be written)"


def _in_real_root(run: Run, path: Path) -> Path | None:
    for root in run.real_roots:
        if _inside(path, root):
            return root
    return None


def check_replica_guard(run: Run, stage: Mapping[str, Any]) -> None:
    """A replica-only run writes nothing inside a root registered as real, and its replica spec binds none."""

    if run.values["ceremony"] != REPLICA_ONLY:
        return
    written = [
        *(Path(run.expand(item)) for item in stage.get("outputs", [])),
        *(
            Path(run.expand(stage[key]))
            for key in ("stdout_to", "snapshot_to", "state_snapshot_to")
            if key in stage
        ),
        *(Path(run.expand(item)) for item in stage.get("updates", [])),
        *(Path(run.expand(item)) for item in stage.get("must_not_exist", [])),
    ]
    for path in written:
        root = _in_real_root(run, path)
        if root is not None:
            raise Refusal(
                f"{stage['id']}: replica-only run would write {path}, inside the real root {root}"
            )
    if "replica_spec_guard" in stage:
        spec = _read_json(Path(run.expand(stage["replica_spec_guard"])))
        replica = Path(run.values["replica_workspace"])
        bound = [("coordination_root", spec["coordination_root"])]
        bound += [("state_root", spec.get("state_root", ""))]
        bound += list(spec["roots"].items())
        for name, value in bound:
            if not value:
                continue
            real = _in_real_root(run, Path(value))
            if real is not None or (
                name != "state_root" and not _inside(Path(value), replica)
            ):
                raise Refusal(
                    f"{stage['id']}: the replica spec binds {name}={value}, outside the replica {replica}"
                    + (f" (inside the real root {real})" if real else "")
                )


def check_scheme(run: Run, stage: Mapping[str, Any]) -> None:
    """A stage that exists for one identity scheme refuses the other; a tree-bound release needs its attestation."""

    only = stage.get("only_scheme")
    if only is not None and only != run.scheme:
        raise Refusal(f"{stage['id']} runs only under {only}, this run is {run.scheme}")
    ceremony = stage.get("only_ceremony")
    if ceremony is not None and ceremony != run.values["ceremony"]:
        raise Refusal(
            f"{stage['id']} runs only in a {ceremony} ceremony, this run is {run.values['ceremony']}"
        )
    needs = stage.get("needs_attestation")
    if run.scheme != TREE_SCHEME or not needs:
        return
    # True: the run's coordination root; a template: the root that stage reads (the replica's)
    root = (
        run.expand(needs) if isinstance(needs, str) else run.values["coordination_root"]
    )
    path = Path(root) / ATTESTATION_FILENAME
    if not path.is_file():
        raise Refusal(
            f"{stage['id']}: merge attestation is mandatory for a tree-bound release ({path} is missing)"
        )
    attestation = _read_json(path)
    if attestation.get("tree") != run.values["code_commit"]:
        raise Refusal(f"{stage['id']}: merge attestation names another tree")
    if attestation.get("merged_commit") != run.repo_identity()["commit"]:
        raise Refusal(f"{stage['id']}: merge attestation names another merged commit")


def check_interpreter(run: Run, stage: Mapping[str, Any]) -> None:
    """A stage that runs the run's Python runs the one the graph requires (``required_python``).

    Every figure the stages hash into pins and generation ids is floating point, and CPython 3.12 changed the
    last digits of ``sum()``: an interpreter other than the required one yields other bytes.
    """

    required = run.graph.get("required_python")
    argv = stage.get("argv") or []
    if not required or run.offline or not argv or argv[0] != "{python}":
        return
    python = run.expand(argv[0])
    wanted = tuple(int(part) for part in str(required).split("."))
    try:
        require_interpreter(
            run.interpreter_version(python),
            what=f"stage {stage['id']}",
            executable=python,
            required=(wanted[0], wanted[1]),
        )
    except InterpreterRefusal as exc:
        raise Refusal(str(exc)) from exc


def check_pinned_inputs(run: Run, stage: Mapping[str, Any]) -> None:
    """Inputs bound to this cycle by a sha256 the operator pinned (a carried-over file from another pass differs)."""

    for path_template, sha_template in stage.get("pinned_inputs", []):
        file = Path(run.expand(path_template))
        if not file.is_file():
            raise Refusal(f"{stage['id']}: pinned input {file} does not exist")
        if _file_sha(file) != run.expand(sha_template):
            raise Refusal(
                f"{stage['id']}: {file.name} is not the file pinned for this run (carried over from another pass?)"
            )


def check_predecessor(run: Run, stage: Mapping[str, Any]) -> None:
    """The predecessor the disclosures compare against is the committed release and panel, byte for byte."""

    rule = stage.get("predecessor_check")
    if rule is None:
        return
    pin = _read_json(Path(run.expand(rule["inputs"])))["predecessor_comparison"]
    current = _read_json(Path(run.expand(rule["current_release"])))
    panel = _read_json(Path(run.expand(rule["panel_current"])))
    if current.get("state") != "COMMITTED":
        raise Refusal(f"{stage['id']}: CURRENT_RELEASE is not COMMITTED")
    if pin["release_id"] != current["release_id"]:
        raise Refusal(
            f"{stage['id']}: predecessor pin names release {pin['release_id'][:12]}, "
            f"CURRENT_RELEASE is {current['release_id'][:12]}"
        )
    if pin["panel_generation_id"] != panel["generation_id"]:
        raise Refusal(
            f"{stage['id']}: predecessor pin names panel {pin['panel_generation_id'][:12]}, "
            f"the current panel is {panel['generation_id'][:12]}"
        )
    data = Path(run.expand(rule["workspace"])) / pin["data_file"]["relative_path"]
    if not data.is_file() or _file_sha(data) != pin["data_file"]["sha256"]:
        raise Refusal(
            f"{stage['id']}: the predecessor data file {data} is missing or differs from its pin"
        )


def check_expected_baseline(run: Run, stage: Mapping[str, Any]) -> None:
    """The acceptance baseline is for this run's financial cutoff."""

    rule = stage.get("expected_baseline")
    if rule is None:
        return
    cutoff = _read_json(Path(run.expand(rule["file"])))["cutoff"]
    if cutoff != run.expand(rule["cutoff"]):
        raise Refusal(
            f"{stage['id']}: expected baseline is for cutoff {cutoff}, the run's is {run.expand(rule['cutoff'])}"
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
    # a replica-only run may READ the real roots through arguments (the replica copy and its spec bind
    # them); a stage that EXECUTES a spec (preflight, plan, dry-run) writes under the roots the spec names,
    # so a spec bound to a real root stays refused (``guard_spec``) and ``check_replica_guard`` refuses
    # every declared output
    keys, spec_template = stage.get("live_guard_keys", []), stage.get("guard_spec")
    if run.values["ceremony"] == REPLICA_ONLY:
        keys = []
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


def _committed(run: Run) -> bool:
    """The run's release has been committed (its ``commit`` stage completed)."""

    record = run.state["stages"].get("commit")
    return bool(record) and record.get("status") == "ok"


def _verify_upstream(run: Run, stage: Mapping[str, Any], seen: set[str]) -> None:
    """Every stage this one requires, and theirs in turn, completed and still has the bytes it recorded.

    ``--only`` therefore cannot start from an input that changed after a prerequisite ran.
    """

    state = run.state["stages"]
    for required in stage.get("requires", []):
        if required in seen:
            continue
        seen.add(required)
        if not _applies(run, required):
            continue
        record = state.get(required)
        if not record or record.get("status") != "ok":
            raise Refusal(f"{stage['id']} requires {required}, which has not completed")
        if record["code_commit"] != run.values["code_commit"]:
            raise Refusal(
                f"{required} ran at {record['code_commit'][:12]}, not the run's code identity"
            )
        verify_kept(run, run.stage(required), record)
        _verify_upstream(run, run.stage(required), seen)


def check_release_inputs(run: Run, stage: Mapping[str, Any]) -> None:
    """The release-inputs files a stage reads (they are not in Git) are in the pinned set."""

    wanted = stage.get("release_inputs")
    if not wanted:
        return
    inputs = run.release_inputs
    if inputs is None:
        if run.offline:
            return
        raise Refusal(
            f"{stage['id']} reads release inputs: the run names no release_inputs_root"
        )
    missing = [name for name in wanted if name not in inputs.paths]
    if missing:
        raise Refusal(
            f"{stage['id']}: the release-inputs set lacks " + ", ".join(missing)
        )


def check_needs_config(run: Run, stage: Mapping[str, Any]) -> None:
    """The run configuration keys only this stage reads are set (other stages run without them)."""

    missing = [key for key in stage.get("needs_config", []) if key not in run.values]
    if missing:
        raise Refusal(
            f"{stage['id']} needs the run configuration key(s) {', '.join(missing)}, which this "
            "configuration does not set"
        )


def check_gates(run: Run, stage: Mapping[str, Any]) -> None:
    """The stage's upfront-QC gates: ``needs_gate`` receipts (PASS, same config and code identity) and ``recheck``
    rows run live now (pointers move between the receipt and the stage, R3 refusal 15)."""

    needs, recheck = stage.get("needs_gate", []), stage.get("recheck", [])
    if not (needs or recheck) or run.offline:
        return
    import release_preflight

    for gate in needs:
        release_preflight.verify_receipt(run, gate)
    if recheck:
        release_preflight.recheck(run, recheck)


def check_ready(
    run: Run,
    stage: Mapping[str, Any],
    *,
    replace: bool,
    resuming: bool = False,
) -> None:
    """Every refusal that applies before ``stage`` runs. Raises ``Refusal``; changes nothing."""

    check_needs_config(run, stage)
    check_interpreter(run, stage)
    run.require_frozen_repo()
    check_config_paths(run)
    check_no_live_stage(run)
    check_gates(run, stage)
    if "argv" in stage:
        check_script_in_repo(run, stage, stage_argv(run, stage))
    check_probe_guard(run, stage)
    check_commit_gate(run, stage)
    check_release_inputs(run, stage)
    if "only_mode" in stage and run.values["mode"] != stage["only_mode"]:
        raise Refusal(f"{stage['id']} runs only in {stage['only_mode']} mode")
    state = run.state["stages"]
    _verify_upstream(run, stage, set())
    own = state.get(stage["id"])
    for path in _claimed_outputs(run, stage):
        if path.exists() and not replace and not resuming:
            where = "an earlier attempt of this run" if own else "an earlier pass"
            raise Refusal(
                f"{stage['id']} would collide with {path}, left by {where}; rerun with --replace"
            )
    check_bound_ids(run, stage)
    check_inventory(run, stage)
    check_scheme(run, stage)
    check_replica_guard(run, stage)
    run.recheck_release_inputs()
    check_pinned_inputs(run, stage)
    check_predecessor(run, stage)
    check_expected_baseline(run, stage)
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
    for item in stage_inputs(run, stage):
        if not Path(run.expand(item)).exists():
            raise Refusal(f"{stage['id']}: input {run.expand(item)} does not exist")


def stage_inputs(run: Run, stage: Mapping[str, Any]) -> list[str]:
    """Input path templates: the stage's own, plus those it reads only for one run configuration value.

    ``config_inputs`` maps a run configuration key to ``{value: [paths]}``; a string configuration
    value that is a key of that map adds its paths, so a stage that reads a file only in one mode
    records (and, when kept, rechecks) that file only then.
    """

    found = list(stage.get("inputs", []))
    for key, by_value in stage.get("config_inputs", {}).items():
        value = run.config.get(key)
        if isinstance(value, str) and value in by_value:
            found.extend(by_value[value])
    return found


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
        if "sha256_of" in check:
            target = Path(run.expand(check["sha256_of"]))
            expected = run.expand(check["equals"])
            if not target.is_file() or _file_sha(target) != expected:
                failures.append(
                    f"{target} does not hash to the pinned {expected[:12]} "
                    "(the run configuration pins another file)"
                )
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
        else:
            expected = check["equals"]
            if isinstance(expected, str):
                expected = run.expand(expected)
            if actual != expected:
                failures.append(
                    f"{file}: {check['path']} is {actual!r}, expected {expected!r}"
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


FROM_LEDGER = "from_ledger"
PROVIDER_ROOT_PREFIX = "provider-"


_LINK_REPARSE_TAGS = (0xA000000C, 0xA0000003)


def _link_like(path: Path) -> bool:
    """A symlink, or on Windows a junction (not an OneDrive placeholder)."""

    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        return getattr(path.lstat(), "st_reparse_tag", 0) in _LINK_REPARSE_TAGS
    except OSError:
        return False


def _resolved_cycle(run: Run) -> dict[str, Any]:
    """The resolved cycle (``release_cycle_manifest resolve --out``): the cycle manifest's root keys, resolved."""

    if "resolved_cycle" not in run.graph.get("paths", {}):
        raise Refusal(
            "provider-generations: from_ledger needs the graph path resolved_cycle"
        )
    path = Path(run.expand("{resolved_cycle}"))
    if not path.is_file():
        raise Refusal(
            f"provider-generations: from_ledger needs the resolved cycle at {path} "
            "(written by the cycle manifest tool's resolve command)"
        )
    resolved = _read_json(path)
    if (
        not isinstance(resolved, dict)
        or resolved.get("schema_version") != release_cycle_manifest.RESOLVED_VERSION
        or not isinstance(resolved.get("providers"), dict)
    ):
        raise Refusal(f"provider-generations: {path} is not a resolved release cycle")
    return resolved


def _sources_from_ledger(run: Run, wanted: Sequence[str]) -> dict[str, str]:
    """Derive the provider source directories from the cycle manifest's roots, not a typed map.

    Each ``provider-<id>`` root's source is the ``generation_dir`` the resolved cycle carries for the
    provider: ``<root of the cycle manifest's root key>/generations/<generation_id>``, one place per
    provider, declared in the cycle manifest. The ledger spec (``{cycle}/ledger-spec.json``, built
    from the same manifest) must agree on root, generation id and manifest hash, and the ledger's own
    staged catalog (graph path ``ledger_catalog``) must admit that generation id, or the stage
    refuses. The directory must be the generation named by its own id inside the provider root,
    whatever links lie on the way. There is no per-run override: a provider whose bytes live in
    another root is declared that way in the cycle manifest.
    """

    spec = _read_json(Path(run.expand("{cycle}/ledger-spec.json")))
    if "ledger_catalog" not in run.graph.get("paths", {}):
        raise Refusal(
            "provider-generations: from_ledger needs the graph path ledger_catalog"
        )
    resolved = _resolved_cycle(run)
    catalog = _read_json(Path(run.expand("{ledger_catalog}")))
    admitted = {
        item["provider_id"]: item["generation_id"]
        for item in catalog.get("provider_generations", [])
    }
    own = set(run.graph.get("provider_run_sources", {}))
    sources: dict[str, str] = {}
    for root_id in wanted:
        if root_id in own:
            continue
        if not root_id.startswith(PROVIDER_ROOT_PREFIX):
            raise Refusal(f"provider-generations: cannot derive a source for {root_id}")
        provider_id = root_id[len(PROVIDER_ROOT_PREFIX) :]
        ref = spec.get("providers", {}).get(provider_id)
        if ref is None:
            raise Refusal(
                f"provider-generations: ledger spec has no provider {provider_id}"
            )
        cycle_ref = resolved["providers"].get(provider_id)
        if not isinstance(cycle_ref, dict):
            raise Refusal(
                f"provider-generations: resolved cycle has no provider {provider_id}"
            )
        generation_id = ref["generation_id"]
        if admitted.get(provider_id) != generation_id:
            raise Refusal(
                f"provider-generations: {provider_id} ledger spec names {generation_id[:12]} "
                f"but the ledger catalog admitted {str(admitted.get(provider_id))[:12]}"
            )
        if cycle_ref.get("generation_id") != generation_id or cycle_ref.get(
            "manifest_sha256"
        ) != ref.get("manifest_sha256"):
            raise Refusal(
                f"provider-generations: {provider_id} resolved cycle and ledger spec name "
                "another generation or manifest"
            )
        root = Path(str(cycle_ref.get("root")))
        if root.resolve() != Path(ref["root"]).resolve():
            raise Refusal(
                f"provider-generations: {provider_id} ledger spec root {ref['root']} is not the "
                f"cycle manifest root {cycle_ref.get('root')}; rebuild the ledger spec from the "
                "resolved cycle"
            )
        directory = Path(str(cycle_ref.get("generation_dir")))
        if (
            directory.name != generation_id
            or directory.resolve() != root.resolve() / "generations" / generation_id
            # the provider root, its generations directory and the generation itself: no link among them
            or any(
                _link_like(part)
                for part in (directory, directory.parent, directory.parent.parent)
            )
        ):
            raise Refusal(
                f"provider-generations: {root_id} source {directory} is not generation "
                f"{generation_id[:12]} inside {root}"
            )
        sources[root_id] = str(directory)
    return sources


def provider_generations(run: Run, target: Path) -> dict[str, Any]:
    """Write the provider-generations list the production spec binds; refuse an incomplete one."""

    wanted = list(run.graph["provider_roots"])
    if "real_root_overrides" in run.config:
        raise Refusal(
            "provider-generations: real_root_overrides is retired; declare the provider's root "
            "in the cycle manifest (the resolved cycle drives the sources)"
        )
    configured = run.config.get("provider_sources", {})
    if configured == FROM_LEDGER:
        sources = _sources_from_ledger(run, wanted)
    elif isinstance(configured, str):
        raise Refusal(
            f"provider-generations: provider_sources must be a map or {FROM_LEDGER!r}"
        )
    else:
        sources = dict(configured)
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


def verify_kept(
    run: Run,
    stage: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    check_binding: bool = True,
    predecessor: bool | None = None,
) -> None:
    """A completed stage is reusable only if the run, the code and its bytes are still what it recorded.

    ``predecessor`` defaults to on until the run's ``commit`` stage has completed and is off from then on, for
    every kept stage however it is reached (upstream of a selected stage, or selected itself): the predecessor pin names the release the run
    replaced, and ``CURRENT_RELEASE`` is the run's own release from then on, so the pin can no longer match
    it; the stages before the commit already checked it, at the moment it was true.
    """

    run.require_frozen_repo()
    if predecessor is None:
        predecessor = not _committed(run)
    if check_binding and record.get("binding") != run.stage_binding(stage["id"]):
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
                f"{stage['id']}: {path} changed or vanished since the stage recorded it; rerun it with --only {stage['id']} --replace"
            )
    # the guarded inputs of the stage are rechecked too, not only the paths it recorded
    check_bound_ids(run, stage)
    run.recheck_release_inputs()
    check_pinned_inputs(run, stage)
    if predecessor:
        check_predecessor(run, stage)
    check_expected_baseline(run, stage)


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


def _tree_inventory(root: Path) -> dict[str, str]:
    return {
        item.relative_to(root).as_posix(): _file_sha(item)
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def replica_inventory(run: Run, target: Path) -> dict[str, Any]:
    """Digest of every file the replica copy produced, written before any later stage can change the tree."""

    root = Path(run.values["replica_workspace"])
    inventory = _tree_inventory(root)
    _write_json(target, {"root": str(root), "files": inventory})
    return {"files": len(inventory)}


def check_inventory(run: Run, stage: Mapping[str, Any]) -> None:
    """The replica tree is still the copy that was inventoried, except for the paths named as expected to change."""

    rule = stage.get("verify_inventory")
    if rule is None:
        return
    recorded = _read_json(Path(run.expand(rule["file"])))["files"]
    root = Path(run.expand(rule["root"]))
    skipped = {
        Path(run.expand(item)).resolve().relative_to(root.resolve()).as_posix()
        for item in rule.get("except", [])
    }
    now = _tree_inventory(root)
    changed = sorted(
        name
        for name in set(recorded) | set(now)
        if name not in skipped and recorded.get(name) != now.get(name)
    )
    if changed:
        raise Refusal(
            f"{stage['id']}: the replica no longer matches its copy inventory ({len(changed)} path(s), e.g. {changed[:3]}); "
            "recopy it with --only replica-copy --replace"
        )


def remove_replica_attestation(run: Run, target: Path) -> dict[str, Any]:
    """Drop the attestation the replica copy brought along: the replica gets its own, written create-only."""

    path = Path(run.expand("{replica_coordination_root}")) / ATTESTATION_FILENAME
    replica = Path(run.values["replica_workspace"])
    if not _inside(path, replica) or _in_real_root(run, path) is not None:
        raise Refusal(f"refusing to remove {path}: not inside the replica workspace")
    removed = path.is_file()
    if removed:
        path.unlink()
    return {"removed": removed}


def _no_builtin_work(run: Run, target: Path) -> dict[str, Any]:
    return {}


_BUILTINS: dict[str, Callable[[Run, Path], dict[str, Any]]] = {
    "inputs_only": _no_builtin_work,
    "provider_generations": provider_generations,
    "replica_provider_generations": replica_provider_generations,
    "remove_replica_attestation": remove_replica_attestation,
    "replica_inventory": replica_inventory,
}


def check_no_live_stage(run: Run) -> None:
    """Refuse while a stage this run launched is still alive (the PID and start token it recorded)."""

    for stage_id, record in sorted(run.state["stages"].items()):
        if stage_id in run.parallel_live:
            # a sibling of this stage's parallel group, launched by this very process
            continue
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
    with run.lock:
        record["pid"] = child.pid
        record["process_started"] = process_started(child.pid)
        run.save()
    try:
        return child.wait()
    except BaseException:
        if child.poll() is None:
            _kill_tree(child.pid)
        with run.lock:
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
    barrier: threading.Barrier | None = None,
) -> dict[str, Any]:
    """Run one stage. In a parallel group every member passes its refusals and moves its leftovers
    aside, then waits at ``barrier``, before any member creates an output directory."""

    try:
        return _execute_stage(run, stage, replace, resuming, barrier)
    except BaseException:
        if barrier is not None:
            barrier.abort()
        raise


def _execute_stage(
    run: Run,
    stage: Mapping[str, Any],
    replace: bool,
    resuming: bool,
    barrier: threading.Barrier | None,
) -> dict[str, Any]:
    check_ready(run, stage, replace=replace, resuming=resuming)
    outputs = _outputs(run, stage)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    previous = run.state["stages"].get(stage["id"], {})
    if "state_snapshot_to" in stage:
        # the state as it is before this stage: a stage that archives it must not archive itself as running
        with run.lock:
            _write_json(
                Path(run.expand(stage["state_snapshot_to"])),
                {
                    "stages": {
                        key: value
                        for key, value in run.state["stages"].items()
                        if key != stage["id"]
                    }
                },
            )
    if not resuming:
        for path in _claimed_outputs(run, stage):
            if path.exists():
                path.rename(path.with_name(f"{path.name}.prev-{stamp}"))
    if barrier is not None:
        barrier.wait()
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
        "binding": run.stage_binding(stage["id"]),
        "code_commit": run.values["code_commit"],
        "started_at": started_at,
        "attempt": attempt,
        "resumed": resuming,
        "log": str(log_path),
        "outputs": {},
        "inputs": {},
        "failures": [],
    }
    with run.lock:
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
            with run.lock:
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
    with run.lock:
        _finish_record(run, stage, record, failures, elapsed, rc, previous, outputs)
    counters = _counters(run, stage, log_path, *([stdout_to] if stdout_to else []))
    if counters is not None:
        with run.lock:
            record["work_counters"] = counters
            run.save()
    _timing(
        run,
        f"{stage['id']} {record['status']} rc={rc} seconds={elapsed}"
        + (f" counters={json.dumps(counters, sort_keys=True)}" if counters else ""),
    )
    return record


def _finish_record(
    run: Run,
    stage: Mapping[str, Any],
    record: dict[str, Any],
    failures: list[str],
    elapsed: float,
    rc: int,
    previous: Mapping[str, Any],
    outputs: Sequence[Path],
) -> None:
    record.update(
        {
            "status": "ok" if not failures else "failed",
            "inputs": {
                str(Path(run.expand(item))): path_digest(Path(run.expand(item)))
                for item in stage_inputs(run, stage)
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
    run.state["stages"][stage["id"]] = record
    run.save()


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
    config_path: Path,
    graph_path: Path | None,
    *,
    offline: bool = False,
    bind_inputs: bool = True,
) -> Run:
    config = _read_json(config_path)
    graph = _read_json(graph_path or Path(config.get("stage_graph") or GRAPH_DEFAULT))
    validate_graph(graph)
    run = Run(config, graph, offline=offline, bind_inputs=bind_inputs)
    return run


def _applies(run: Run, stage_id: str) -> bool:
    """False for a stage that exists only for another identity scheme or another ceremony."""

    stage = run.stage(stage_id)
    return bool(
        stage.get("only_scheme", run.scheme) == run.scheme
        and stage.get("only_ceremony", run.values["ceremony"]) == run.values["ceremony"]
    )


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
            if (i in needed or not run.stage(i).get("explicit")) and _applies(run, i)
        ]
    return [i for i in ids if not run.stage(i).get("explicit") and _applies(run, i)]


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


def _groups(run: Run, selected: Sequence[str]) -> list[list[str]]:
    """Consecutive selected stages that share a ``parallel_group`` form one batch; every other stage is its own."""

    batches: list[list[str]] = []
    for stage_id in selected:
        group = run.stage(stage_id).get("parallel_group")
        if (
            group is not None
            and batches
            and run.stage(batches[-1][0]).get("parallel_group") == group
        ):
            batches[-1].append(stage_id)
        else:
            batches.append([stage_id])
    return batches


def _report(run: Run, stage: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    print(f"{stage['id']}: {result['status']} in {result['seconds']}s", flush=True)
    if result["status"] != "ok":
        for failure in result["failures"]:
            print(f"  {failure}", file=sys.stderr)
        print(f"  log: {result['log']}", file=sys.stderr)
    elif stage.get("announce_release"):
        # the owner can read the release id and the findings while the sibling stage is still running
        try:
            print(
                f"{stage['id']}: release {run.derive('release_id')} is PREPARED on the real roots; "
                "the commit stays gated on the other stages of this group",
                flush=True,
            )
        except Refusal as refusal:
            print(f"{stage['id']}: release id not readable yet ({refusal})", flush=True)


def run_parallel(run: Run, jobs: Sequence[tuple[Mapping[str, Any], bool, bool]]) -> int:
    """Run the stages of one parallel group at the same time; 0 when all are ok, 1 when one failed.

    Every member passes its refusals and moves leftovers aside before any member creates an output
    (``barrier``); a refusal of any member stops the group before a process starts. The state file is
    shared, so state writes are serialized by ``run.lock``.
    """

    # every member's refusals first, before any member moves a leftover aside: one refusal changes nothing
    for stage, replace, resuming in jobs:
        check_ready(run, stage, replace=replace, resuming=resuming)
    barrier = threading.Barrier(len(jobs))
    outcomes: dict[str, Any] = {}
    run.parallel_live.update(str(stage["id"]) for stage, _, _ in jobs)

    def work(stage: Mapping[str, Any], replace: bool, resuming: bool) -> None:
        try:
            result = execute_stage(
                run, stage, replace=replace, resuming=resuming, barrier=barrier
            )
        except threading.BrokenBarrierError:
            outcomes[stage["id"]] = None
            return
        except BaseException as exc:  # noqa: BLE001 - reported by the caller, in stage order
            outcomes[stage["id"]] = exc
            return
        outcomes[stage["id"]] = result
        _report(run, stage, result)
        waiting = [str(s["id"]) for s, _, _ in jobs if s["id"] not in outcomes]
        if waiting:
            print(f"  still running: {', '.join(waiting)}", flush=True)

    threads = [
        threading.Thread(target=work, args=job, name=str(job[0]["id"]), daemon=True)
        for job in jobs
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        for name in stop_own(run):
            print(f"stopped {name}", file=sys.stderr)
        raise
    finally:
        run.parallel_live.clear()
    errors = [value for value in outcomes.values() if isinstance(value, BaseException)]
    for error in errors:
        if isinstance(error, Refusal):
            raise error
    for error in errors:
        raise error
    # a member that never started (its barrier broke) is a failure, not a pass
    return (
        1
        if any(value is None or value["status"] != "ok" for value in outcomes.values())
        else 0
    )


def run_selected(
    run: Run, selected: Sequence[str], *, only: str | None, replace: bool
) -> int:
    for batch in _groups(run, selected):
        jobs: list[tuple[Mapping[str, Any], bool, bool]] = []
        for stage_id in batch:
            stage = run.stage(stage_id)
            record = run.state["stages"].get(stage_id)
            if record and record["status"] == "ok" and only is None:
                verify_kept(run, stage, record)
                print(f"{stage_id}: ok (kept)")
                continue
            resuming = bool(
                record
                and record["status"] in ("failed", "interrupted")
                and "resume" in stage
                and not replace
            )
            jobs.append((stage, replace or (bool(record) and not resuming), resuming))
        if len(jobs) > 1:
            print(
                f"parallel group {jobs[0][0]['parallel_group']}: "
                + ", ".join(str(stage["id"]) for stage, _, _ in jobs)
                + f" start {_now()}",
                flush=True,
            )
            if run_parallel(run, jobs) != 0:
                return 1
            continue
        for stage, stage_replace, resuming in jobs:
            print(
                f"{stage['id']}: {'resume' if resuming else 'start'} {_now()}",
                flush=True,
            )
            result = execute_stage(run, stage, replace=stage_replace, resuming=resuming)
            _report(run, stage, result)
            if result["status"] != "ok":
                return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=("plan", "run", "status", "check", "gate-commit", "stop", "rerecord"),
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
        "--confirm-same-config",
        action="store_true",
        help="rerecord: the run configuration and stage definitions have not changed what these stages produced",
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
        # stop and status never consume release inputs: an unmounted root must not block the kill path
        run = load_run(
            args.config,
            args.graph,
            offline=args.offline,
            bind_inputs=args.command not in ("stop", "status"),
        )
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
            print(mode_line(run), file=sys.stderr)
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
            print(mode_line(run))
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
        if args.command == "rerecord":
            if not args.confirm_same_config:
                raise Refusal(
                    "rerecord re-binds completed stages to the current configuration and graph; "
                    "pass --confirm-same-config after checking that neither changed what they produced"
                )
            # a live stage would later save its older in-memory state over these bindings
            check_no_live_stage(run)
            todo = [i for i in selected if run.state["stages"].get(i)]
            current: dict[str, tuple[dict[str, str | None], dict[str, str | None]]] = {}
            for stage_id in todo:
                record = run.state["stages"][stage_id]
                stage = run.stage(stage_id)
                if record.get("status") != "ok":
                    raise Refusal(
                        f"{stage_id} is {record.get('status')}, not ok: nothing is re-recorded"
                    )
                verify_kept(run, stage, record, check_binding=False)
                # the stage's declared artifacts as the current graph names them: all must exist, and
                # they are what later runs verify, so a newly declared output is bound from now on
                missing = [p for p in _outputs(run, stage) if not p.exists()]
                missing += [
                    Path(run.expand(i))
                    for i in stage.get("inputs", [])
                    if not Path(run.expand(i)).exists()
                ]
                if missing:
                    raise Refusal(
                        f"{stage_id}: declared artifact {missing[0]} does not exist: nothing is re-recorded"
                    )
                current[stage_id] = (
                    {str(p): path_digest(p) for p in _outputs(run, stage)},
                    {
                        str(Path(run.expand(i))): path_digest(Path(run.expand(i)))
                        for i in stage.get("inputs", [])
                    },
                )
            for stage_id in todo:
                record = run.state["stages"][stage_id]
                record["outputs"], record["inputs"] = current[stage_id]
                record["binding"] = run.stage_binding(stage_id)
                record["rerecorded_at"] = _now()
                print(f"{stage_id}: re-recorded")
            run.save()
            return 0
        if args.command == "stop":
            for line in stop_own(run) or ["nothing of this run is running"]:
                print(line)
            return 0
        print(mode_line(run), flush=True)
        for stage_id in reconcile_state(run):
            print(f"{stage_id}: interrupted earlier; will resume from its own boundary")
        return run_selected(run, selected, only=args.only, replace=args.replace)
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
