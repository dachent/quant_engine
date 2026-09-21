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

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(
    r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
    r"/.codex-bootstrap/mvp2-execution/repo"
)
sys.path[:0] = [str(REPO / "tools")] + [str(p) for p in REPO.glob("packages/*/src")]

Q = Path(r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine")
W = Path(r"C:/Dev/scratch/quant-engine/pl12")
PREPARED = W / "run2"
CYCLE = W / "cycle2"
LIVE = Q / "dataPortfolio/rollforward/2026-09-10-mvp2-live-only"
CONTRACT = REPO / "contracts/portfolio-financial-mvp-source-v1.json"
OUT = W / "production"


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
    vertical = json.loads((CYCLE / "vertical-recipe.json").read_bytes())
    analytics = json.loads((PREPARED / "stage-results/analytics.json").read_bytes())
    lens_root = {
        (row["evidence_mode"], row["surface"], row["source_key"]): Path(row["root"])
        / "generations"
        / row["generation_id"]
        for row in analytics["lens_generation_bindings"]
    }
    report_root = (
        PREPARED
        / "stages/report/generations"
        / recipe["expected_generations"]["report"]["generation_id"]
    )
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
    # The history may be reused rather than rebuilt, so the pinned policy is
    # the authority for which generation this cycle actually used.
    policy = json.loads(
        (REPO / "contracts/portfolio-dependence-policy-v1.json").read_bytes()
    )
    history_root = (
        LIVE / "layered-history" / policy["history_authority"]["generation_id"]
    )
    dependence = json.loads((CYCLE / "dependence-final.json").read_bytes())

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

    rows.append(artifact("financial-outcome-hwm", W / "hwm/as-held-composite-hwm.json"))
    rows.append(
        artifact("financial-capital-input", W / "capital/capital-input-generation.json")
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
    base_source = W / "epoch/base-source"
    index = 0
    for child in sorted(base_source.rglob("*")):
        if child.is_file():
            rows.append(artifact("decomposition-base-source-%d" % index, child))
            index += 1

    # The correlation replay reads its sources by digest from the dependence
    # bundle, so every file in it has to be nameable.
    dependence_root = LIVE / "dependence" / dependence["bundle_id"]
    index = 0
    for child in sorted(dependence_root.rglob("*")):
        if child.is_file():
            rows.append(artifact("dependence-source-%d" % index, child))
            index += 1

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "financial-artifacts.json"
    path.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")
    print("artifact rows", len(rows))
    print(
        "  decomposition cells",
        sum(1 for value in roles.values() if value[0] == "DECOMPOSITION"),
    )
    print(
        "  dependence cells",
        sum(1 for value in roles.values() if value[0] == "DEPENDENCE"),
    )
    print("  dependence sources", index)
    print("written", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
