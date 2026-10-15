"""Enforce docs/architecture/system-boundaries.md with a shrinking allowlist.

One pass over the tracked production Python files (tests and
``ops/preserved-scripts`` are excluded) produces violations of six rules:

  LAYER          a package imports across the allowed edges, or imports a tools,
                 ops, acceptance or app module; tools import ops/acceptance/apps
  PRIVATE        ``from <first-party> import _x`` and ``<first-party module>._x``
                 across owners
  SUBMODULE      ``quant_engine.<pkg>.<sub>`` imported from outside ``<pkg>``
  DYNAMIC        spec_from_file_location, exec_module, sys.path writes, a
                 subprocess or ``_tool(...)`` call that runs a ``tools/*.py`` file
  UNDECLARED     a package or app imports a third-party module it does not declare
  PROVIDER_NAME  a string constant that is exactly a provider id, in a file whose
                 own name is not provider specific

Today's violations are recorded per (rule, file) in
``.policy/architecture-allowlist.json``. The check passes only when the census
matches the allowlist exactly: a new violation fails, and so does a removed one
until ``--update-allowlist`` ratchets the file down. The updater refuses to
raise any count or add any entry. The same pass writes the allowed-edge contract
and the as-built import graph (``--write-docs``).
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / ".policy" / "architecture-allowlist.json"
EDGES = ROOT / "docs" / "architecture" / "allowed-edges.json"
GRAPH = ROOT / "docs" / "architecture" / "import-graph.json"

PROVIDERS = (
    "alpaca",
    "fidelity",
    "ibkr",
    "matrixport",
    "portfolio123",
    "schwab",
    "turingtrader",
)
PROVIDER_IDS = frozenset((*PROVIDERS, "quantbase", "robuxio", "p123"))
# Dependency rules from system-boundaries.md: core has no provider dependency,
# domain packages never import providers, providers never import each other.
ALLOWED_EDGES: Mapping[str, tuple[str, ...]] = {
    "core": (),
    "ci_controls": (),
    "portfolio_ledger": ("core",),
    "web_contracts": ("core",),
    **{name: ("core", "portfolio_ledger", "web_contracts") for name in PROVIDERS},
}
APP_BANNED_FOR_TOOLS = ("acceptance", "ops")
ALIASES = {
    "yaml": "pyyaml",
    "pil": "pillow",
    "dateutil": "python_dateutil",
    "schwab": "schwab_py",
}
SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
TOOLS_PATH = re.compile(r"(^|[\\/])tools[\\/][\w.-]+\.py|^tools$", re.IGNORECASE)
RULES = ("LAYER", "PRIVATE", "SUBMODULE", "DYNAMIC", "UNDECLARED", "PROVIDER_NAME")

Violation = tuple[str, str, int, str]  # rule, file, line, detail


def tracked_python(root: Path = ROOT) -> dict[str, str]:
    """Tracked production sources, keyed by repo-relative posix path."""

    listing = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\n")
    files: dict[str, str] = {}
    for path in filter(None, listing):
        parts = path.split("/")
        if (
            "tests" in parts
            or parts[-1] == "conftest.py"
            or parts[-1].startswith("test_")
        ):
            continue
        if parts[:2] == ["ops", "preserved-scripts"]:
            continue
        if (root / path).is_file():
            files[path] = (root / path).read_bytes().decode("utf-8", "replace")
    return files


def _owner(path: str) -> str:
    parts = path.split("/")
    if parts[0] == "packages" and len(parts) > 4 and parts[2] == "src":
        return "pkg:" + parts[4].removesuffix(".py")
    if parts[0] == "tools" and len(parts) == 2:
        return "tools:" + parts[1].removesuffix(".py")
    if parts[0] == "apps":
        return "app:" + parts[1]
    return parts[0]


def _imports(tree: ast.AST) -> Iterable[tuple[int, str, tuple[str, ...]]]:
    """Yield (line, absolute module, imported names); relative imports are skipped."""

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, ()
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module, tuple(a.name for a in node.names)


def _declared(root: Path, project_dir: str) -> set[str] | None:
    path = root / project_dir / "pyproject.toml"
    if not path.is_file():
        return None
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    names = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        names.extend(extra)
    declared = set()
    for spec in names:
        match = re.match(r"[A-Za-z0-9_.-]+", spec)
        if match:
            declared.add(re.sub(r"[-.]+", "_", match.group().lower()))
    return declared


def census(files: Mapping[str, str], root: Path = ROOT) -> list[Violation]:
    owners = {path: _owner(path) for path in files}
    tools = {o[6:] for o in owners.values() if o.startswith("tools:")}
    pkg_modules = set()
    for p in files:
        if p.startswith("packages/") and "/src/" in p:
            rel = p.split("/src/", 1)[1].removesuffix(".py").removesuffix("/__init__")
            pkg_modules.add(rel.replace("/", "."))
    app_modules = {
        Path(p).stem for p in files if p.startswith("apps/") and p.count("/") == 2
    }
    local_names: dict[str, set[str]] = {}
    found: list[Violation] = []
    for path, source in sorted(files.items()):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        owner = owners[path]
        imports = list(_imports(tree))
        bound: dict[str, str] = {}

        def first_party(module: str) -> str | None:
            top = module.split(".")[0]
            if top == "quant_engine":
                return "pkg:" + (module.split(".") + ["", ""])[1]
            return f"tools:{top}" if top in tools else None

        for line, module, names in imports:
            target = first_party(module)
            parts = module.split(".")
            top = parts[0]
            if owner.startswith("pkg:"):
                this = owner[4:]
                if target and target.startswith("pkg:") and target[4:] != this:
                    if target[4:] not in ALLOWED_EDGES.get(this, ()):
                        found.append(("LAYER", path, line, f"{this}->{target[4:]}"))
                elif target and target.startswith("tools:"):
                    found.append(("LAYER", path, line, f"{this}->{target}"))
                elif top in (*APP_BANNED_FOR_TOOLS, *app_modules):
                    found.append(("LAYER", path, line, f"{this}->{top}"))
            elif owner.startswith("tools:") and top in APP_BANNED_FOR_TOOLS:
                found.append(("LAYER", path, line, f"{owner}->{top}"))
            if target and target != owner:
                for name in names:
                    if name.startswith("_") and not name.startswith("__"):
                        found.append(("PRIVATE", path, line, f"{module}.{name}"))
                if target.startswith("pkg:"):
                    if len(parts) > 2:
                        found.append(("SUBMODULE", path, line, module))
                    else:
                        for name in names:
                            if f"{module}.{name}" in pkg_modules:
                                found.append(
                                    ("SUBMODULE", path, line, f"{module}.{name}")
                                )
        # PRIVATE via module attribute: `module._x` where `module` is first-party.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = first_party(alias.name)
                    if target and target != owner and "." not in alias.name:
                        bound[alias.asname or alias.name] = target
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                target = first_party(node.module)
                if target and target != owner:
                    for alias in node.names:
                        if f"{node.module}.{alias.name}" in pkg_modules:
                            bound[alias.asname or alias.name] = target
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in bound
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
            ):
                found.append(
                    (
                        "PRIVATE",
                        path,
                        node.lineno,
                        f"{bound[node.value.id]}.{node.attr}",
                    )
                )
        # DYNAMIC
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                if name in {"spec_from_file_location", "exec_module"}:
                    found.append(("DYNAMIC", path, node.lineno, name))
                elif name in {"insert", "append", "extend", "remove"} and ast.unparse(
                    func
                ).startswith("sys.path"):
                    found.append(("DYNAMIC", path, node.lineno, "sys.path"))
                elif name == "_tool":
                    found.append(("DYNAMIC", path, node.lineno, "_tool"))
                elif name in SUBPROCESS_CALLS and ast.unparse(func).startswith(
                    "subprocess"
                ):
                    strings = [
                        n.value
                        for n in ast.walk(node)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    ]
                    if any(TOOLS_PATH.search(s) for s in strings):
                        found.append(("DYNAMIC", path, node.lineno, "subprocess tools"))
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for tgt in targets:
                    if ast.unparse(tgt).startswith("sys.path"):
                        found.append(("DYNAMIC", path, node.lineno, "sys.path"))
        # UNDECLARED (package and app sources only)
        project = None
        if owner.startswith("pkg:"):
            project = "/".join(path.split("/")[:2])
        elif owner.startswith("app:"):
            project = "/".join(path.split("/")[:2])
        if project is not None:
            declared = _declared(root, project)
            if declared is not None:
                locals_ = local_names.setdefault(
                    project,
                    {Path(p).stem for p in files if p.startswith(project + "/")},
                )
                for line, module, _ in imports:
                    top = module.split(".")[0]
                    norm = re.sub(r"[-.]+", "_", top.lower())
                    if (
                        top in sys.stdlib_module_names
                        or top == "quant_engine"
                        or top in locals_
                        or top in tools
                        or norm in declared
                        or ALIASES.get(norm) in declared
                        or "quant_engine_" + norm in declared
                    ):
                        continue
                    found.append(("UNDECLARED", path, line, top))
        # PROVIDER_NAME
        if (
            owner.startswith("tools:")
            and owner != "tools:check_architecture"  # defines the provider ids
            and not any(p in owner for p in PROVIDER_IDS)
        ):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.strip().lower() in PROVIDER_IDS
                ):
                    found.append(("PROVIDER_NAME", path, node.lineno, node.value))
    return found


def counts(violations: Iterable[Violation]) -> dict[str, dict[str, int]]:
    table: dict[str, Counter[str]] = {rule: Counter() for rule in RULES}
    for rule, path, _, _ in violations:
        table[rule][path] += 1
    return {rule: dict(sorted(table[rule].items())) for rule in RULES}


def compare(
    actual: Mapping[str, Mapping[str, int]], allowed: Mapping[str, Mapping[str, int]]
) -> tuple[list[str], list[str]]:
    """Return (new violations, stale allowlist entries)."""

    new: list[str] = []
    stale: list[str] = []
    for rule in RULES:
        have, may = actual.get(rule, {}), allowed.get(rule, {})
        for path in sorted(set(have) | set(may)):
            if have.get(path, 0) > may.get(path, 0):
                new.append(
                    f"{rule} {path}: {have.get(path, 0)} > allowed {may.get(path, 0)}"
                )
            elif have.get(path, 0) < may.get(path, 0):
                stale.append(
                    f"{rule} {path}: {have.get(path, 0)} < allowed {may[path]}"
                )
    return new, stale


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def edges_contract() -> dict[str, Any]:
    return {
        "source": "docs/architecture/system-boundaries.md (Dependency rules)",
        "generated_by": "tools/check_architecture.py --write-docs",
        "allowed_package_imports": {
            k: list(v) for k, v in sorted(ALLOWED_EDGES.items())
        },
        "tools_must_not_import": list(APP_BANNED_FOR_TOOLS),
        "packages_must_not_import": ["tools modules", "ops", "acceptance", "apps"],
    }


def import_graph(files: Mapping[str, str]) -> dict[str, Any]:
    """As-built edges: package->package and tools->tools, one record per pair."""

    tools = {_owner(p)[6:] for p in files if _owner(p).startswith("tools:")}
    pkg_edges: Counter[tuple[str, str]] = Counter()
    tool_edges: Counter[tuple[str, str]] = Counter()
    for path, source in files.items():
        owner = _owner(path)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for _, module, _ in _imports(tree):
            parts = module.split(".")
            if (
                owner.startswith("pkg:")
                and parts[0] == "quant_engine"
                and len(parts) > 1
            ):
                if parts[1] != owner[4:]:
                    pkg_edges[(owner[4:], parts[1])] += 1
            elif (
                owner.startswith("tools:")
                and parts[0] in tools
                and parts[0] != owner[6:]
            ):
                tool_edges[(owner[6:], parts[0])] += 1
    return {
        "generated_by": "tools/check_architecture.py --write-docs",
        "package_edges": [[a, b, n] for (a, b), n in sorted(pkg_edges.items())],
        "tools_edges": [[a, b, n] for (a, b), n in sorted(tool_edges.items())],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--init", action="store_true", help="create the allowlist (once)"
    )
    parser.add_argument("--update-allowlist", action="store_true", help="ratchet down")
    parser.add_argument(
        "--write-docs", action="store_true", help="write edge contract and graph"
    )
    parser.add_argument("--detail", action="store_true", help="list every violation")
    args = parser.parse_args(argv)

    files = tracked_python()
    violations = census(files)
    actual = counts(violations)
    if args.detail:
        for rule, path, line, detail in sorted(violations):
            print(f"{rule} {path}:{line} {detail}")
    if args.write_docs:
        EDGES.write_bytes(_dump(edges_contract()).encode())
        GRAPH.write_bytes(_dump(import_graph(files)).encode())
    if args.init:
        if ALLOWLIST.exists():
            print("allowlist exists; use --update-allowlist", file=sys.stderr)
            return 2
        ALLOWLIST.write_bytes(_dump(actual).encode())
        print(f"wrote {ALLOWLIST.relative_to(ROOT)}")
        return 0
    allowed = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    new, stale = compare(actual, allowed)
    if args.update_allowlist:
        if new:
            print(
                "allowlist may only shrink; fix these first:\n  " + "\n  ".join(new),
                file=sys.stderr,
            )
            return 1
        ALLOWLIST.write_bytes(_dump(actual).encode())
        print(f"ratcheted down {len(stale)} entries")
        return 0
    if json.loads(EDGES.read_text(encoding="utf-8")) != edges_contract():
        print("allowed-edges.json is stale; run --write-docs", file=sys.stderr)
        return 1
    if new or stale:
        if new:
            print(
                "NEW architecture violations (fix them; the allowlist may not grow):",
                file=sys.stderr,
            )
            for msg in new:
                print("  " + msg, file=sys.stderr)
        if stale:
            print(
                "Allowlist entries no longer violated (run --update-allowlist):",
                file=sys.stderr,
            )
            for msg in stale:
                print("  " + msg, file=sys.stderr)
        return 1
    total = sum(sum(v.values()) for v in actual.values())
    print(f"architecture: PASS ({total} allowlisted violations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
