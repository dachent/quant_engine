"""Assemble the ArtifactNode inventory the production specification requires.

`build_production_spec` already binds the release roots, the current lens
generations and the product gates. What it cannot derive is the finite set of
numerical outcome artifacts and the producer inputs they are bound to, because
those live in bundles and generations outside the release roots. This script
enumerates exactly the roles `required_outcome_roles` derives from the contract
and the selected decomposition plan, plus the plan bundle files, the
layered-history and account-manifest generations, and the dependence sources
the correlation replay reads.

Every row is a definition of a file that already exists: path, sha256 and size
read from disk. Nothing is created here, and a row that is wrong fails closed
in `validate_financial_outcomes` rather than shipping.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _arguments() -> argparse.Namespace:
    """Take every root as an argument.

    The pinned constants this replaced named one cycle's roots -- a repo checkout, a venv and two
    run roots -- and none of them exist any more, so the script could not run at all. They also
    hid which inputs are the run's and which are the product's.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", type=Path, required=True, help="the prepared run's output root"
    )
    parser.add_argument(
        "--cycle",
        type=Path,
        required=True,
        help="where the pipeline recipe was written",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="defaults to the working directory",
    )
    return parser.parse_args()


ARGS = _arguments()
REPO = ARGS.repo.resolve()
sys.path[:0] = [str(REPO / "tools")] + [str(p) for p in REPO.glob("packages/*/src")]

PREPARED = ARGS.run.resolve()
CYCLE = ARGS.cycle.resolve()
CONTRACT = REPO / "contracts/portfolio-financial-mvp-source-v1.json"
OUT = ARGS.out.resolve()


def definition(path):
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def artifact(node_id, path, depends_on=()):
    return {
        **definition(path),
        "depends_on": list(depends_on),
        "kind": "ARTIFACT",
        "node_id": node_id,
    }


def main():
    from portfolio_financial_outcomes import required_outcome_roles

    recipe = json.loads((CYCLE / "pipeline-recipe.json").read_bytes())
    # The attended runner writes the vertical recipe into the run it describes.
    vertical = json.loads((PREPARED / "preparation/release-recipe.json").read_bytes())
    analytics = json.loads((PREPARED / "stage-results/analytics.json").read_bytes())
    lens_root = {
        (row["evidence_mode"], row["surface"], row["source_key"]): Path(row["root"])
        / "generations"
        / row["generation_id"]
        for row in analytics["lens_generation_bindings"]
    }
    # The report generation root is no longer resolved here: the capital outcome now reads
    # the run's own `preparation/invested-capital-result.json`, and nothing else used it.
    correlation_root = (
        Path(analytics["correlation_root"])
        / "generations"
        / analytics["correlation_generation_id"]
    )
    epoch = (
        Path(vertical["epoch_manifest_generation"]["runtime_root"])
        / "generations"
        / (vertical["epoch_manifest_generation"]["generation_id"])
    )
    plan_bundle = Path(vertical["decomposition_plan_bundle"]["path"])
    plan = json.loads(
        (
            plan_bundle / "inputs/governance/current-strategy-decomposition-plan.json"
        ).read_bytes()
    )
    contract = json.loads(CONTRACT.read_bytes())
    # The run's own selected history is the authority, not the pinned policy.
    # `portfolio_current_dependence.py:335-337` reads `policy["history_authority"]` only as a
    # FALLBACK -- `history_authority if ... is not None else policy.get(...)` -- and `:630-652`
    # builds that override from the selected generation's own manifest and passes it. So a run
    # consuming a newer history than the pin is the designed path, and binding the pin here
    # would silently bind whatever the previous cycle used, since that generation is still on
    # disk. The divergence is worth reporting; it is not grounds to refuse.
    selected = json.loads(
        (PREPARED / "preparation/dependence-selected-inputs.json").read_bytes()
    )
    history_root = Path(selected["history_generation_root"])
    policy = json.loads(
        (REPO / "contracts/portfolio-dependence-policy-v1.json").read_bytes()
    )
    pinned_history = policy["history_authority"]["generation_id"]

    rows = []
    # The contract requires DECOMPOSITION in both evidence modes and the
    # consumer resolves one plan bundle per mode, so both have to be nameable.
    # Binding only the primary was correct while the vertical built one bundle.
    bundles = [plan_bundle]
    hybrid_definition = vertical.get("hybrid_decomposition_plan_bundle")
    if hybrid_definition is not None:
        bundles.append(Path(hybrid_definition["path"]))
    seen_modes = set()
    for bundle in bundles:
        manifest = json.loads((bundle / "plan-manifest.json").read_bytes())
        mode = manifest["policies"]["strategy_evidence_mode"]
        mode_id = mode.lower().replace("_", "-")
        if mode_id in seen_modes:
            raise SystemExit("two plan bundles claim evidence mode %s" % mode)
        seen_modes.add(mode_id)
        rows.append(
            artifact(
                "financial-decomposition-plan-" + mode_id, bundle / "plan-manifest.json"
            )
        )
        for index, item in enumerate([*manifest["inputs"], *manifest["outputs"]]):
            rows.append(
                artifact(
                    "financial-decomposition-source-%s-%d" % (mode_id, index),
                    bundle / item["path"],
                )
            )

    rows.append(
        artifact(
            "financial-layered-history-generation",
            history_root / "generation.json",
        )
    )
    rows.append(
        artifact("financial-account-manifest-generation", epoch / "generation.json")
    )

    rows.append(
        artifact(
            "financial-outcome-hwm", PREPARED / "preparation/hwm-governed-result.json"
        )
    )
    rows.append(
        artifact(
            "financial-capital-input",
            PREPARED / "preparation/capital-input-generation.json",
        )
    )
    # Bound to the report generation, because `verify_outcome_inventory` requires every non-HWM
    # outcome path to resolve to a file inside one of its `depends_on` generations. NOTE: this
    # run's report generation does not emit `invested-capital.json` -- it appears only under
    # `stages/report/candidates/` -- so the INVESTED_CAPITAL outcome (#318) has no valid binding
    # in the attended layout yet. That is a product question, not a path fix; do not "correct" it
    # by pointing outside the generation, which silently trades a loud absence for
    # "financial outcome generation binding differs".
    report_root = (
        PREPARED
        / "stages/report/generations"
        / recipe["expected_generations"]["report"]["generation_id"]
    )
    rows.append(
        artifact(
            "financial-outcome-capital",
            report_root / "invested-capital.json",
            ("publish-portfolio-report",),
        )
    )

    roles = required_outcome_roles(contract, plan=plan)
    for role, selection in sorted(roles.items()):
        if selection[0] == "DECOMPOSITION":
            _kind, _strategy, source_key, _lens, surface, mode_key = selection
            key = (mode_key, surface, source_key)
            if key not in lens_root:
                raise SystemExit("no lens generation for %s" % (key,))
            root_id = (
                "hybrid-%s" % source_key
                if mode_key == "LIVE_PLUS_BACKTEST"
                else source_key
            )
            node_id = "publish-%s%s" % (
                root_id,
                "" if surface == "STATIC" else "-dynamic-fls",
            )
            rows.append(
                artifact(role, lens_root[key] / "decomposition.json", (node_id,))
            )
        elif selection[0] == "DEPENDENCE":
            rows.append(
                artifact(
                    role,
                    correlation_root / "correlation.json",
                    ("publish-correlation",),
                )
            )

    # The decomposition base specification the plan bundle carries names its
    # provenance relative to the base source root, and the bundle copies those
    # files in under a different name (`inputs/base/provenance/NN-<name>`), so
    # the original name resolves only against the root itself. Same reason as
    # the dependence bundle below: a source read by digest has to be nameable.
    base_source = PREPARED / "stages/analytics/base-input"
    index = 0
    for child in sorted(base_source.rglob("*")):
        if child.is_file():
            rows.append(artifact("decomposition-base-source-%d" % index, child))
            index += 1

    # The correlation replay reads its sources by digest from the dependence
    # bundle, so every file in it has to be nameable.
    dependence_root = Path(vertical["correlation_source_root"]["path"])
    index = 0
    for child in sorted(dependence_root.rglob("*")):
        if child.is_file():
            rows.append(artifact("dependence-source-%d" % index, child))
            index += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    path = OUT
    path.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")
    print("artifact rows", len(rows))
    print(
        "  layered history",
        history_root.name[:16],
        "(policy pin %s%s)"
        % (
            pinned_history[:16],
            "; the run's own authority wins"
            if history_root.name != pinned_history
            else "",
        ),
    )
    print(
        "  decomposition cells",
        sum(1 for value in roles.values() if value[0] == "DECOMPOSITION"),
    )
    print(
        "  dependence cells",
        sum(1 for value in roles.values() if value[0] == "DEPENDENCE"),
    )
    print("  dependence sources", index)
    print("  recipe", recipe["pipeline_id"], "at", recipe["release_commit"][:8])
    print("written", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
