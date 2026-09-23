"""Emit the predecessor-authority-repair gate for the current committed release.

Every field is derived from disk. The pipeline re-derives the same drift when
it verifies the gate and refuses one that does not match exactly, so this
cannot admit a divergence that is not actually present. It also refuses a
repair whose new release reuses the drifted generation, which is checked here
too so the failure is reported before the production spec is built.

Independent of production-spec.json: the root set comes from the materializer's
own PRODUCTION_ROOTS, and the superseding generations from the prepared root.
"""
import hashlib
import json
import pathlib
import sys

REPO = pathlib.Path(
    "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
    "/.codex-bootstrap/mvp2-execution/repo"
)
sys.path[:0] = [str(REPO / "packages/core/src"), str(REPO / "tools"), str(REPO)]

from quant_engine.core.publication import generation_inventory_sha256  # noqa: E402
from quant_engine.core.release import ReleaseAuthorityPointer  # noqa: E402
from portfolio_pipeline_materialize import PRODUCTION_ROOTS  # noqa: E402

Q = pathlib.Path("C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine")
PORTFOLIO = Q / "dataPortfolio"
COORD = PORTFOLIO / "releases-refresh-2026"
PREPARED = pathlib.Path("C:/Dev/scratch/quant-engine/pl12/run2/release-roots")
OUT = pathlib.Path(
    "C:/Dev/scratch/quant-engine/pl12/production/predecessor-authority-repair.json"
)

authority_bytes = (COORD / "CURRENT_RELEASE.json").read_bytes()
authority = ReleaseAuthorityPointer.from_json_bytes(authority_bytes)
release = json.loads(
    (COORD / "releases" / authority.release_id / "release.json").read_bytes()
)


def staged_generations(root_id: str) -> set[str]:
    """Generation ids the prepared release staged for this root.

    A prepared root carries no CURRENT.json -- the pointer only appears at
    commit -- so the staged generation directories are what identify the
    successor before the commit happens.
    """

    directory = PREPARED / PRODUCTION_ROOTS[root_id] / "generations"
    if not directory.is_dir():
        return set()
    return {child.name for child in directory.iterdir() if child.is_dir()}


drifts = []
for target in sorted(release["targets"], key=lambda t: t["root_id"]):
    root_id = target["root_id"]
    recorded = target.get("proposed_inventory_sha256")
    if recorded is None:
        continue
    root = PORTFOLIO / PRODUCTION_ROOTS[root_id]
    gid = target["proposed_generation_id"]
    observed = generation_inventory_sha256(root, gid)
    if observed == recorded:
        continue
    staged = staged_generations(root_id)
    if gid in staged:
        raise SystemExit(
            f"the new release reuses the drifted generation for {root_id}; "
            "a repair cannot admit that"
        )
    drifts.append(
        {
            "generation_id": gid,
            "observed_inventory_sha256": observed,
            "recorded_inventory_sha256": recorded,
            "root_id": root_id,
        }
    )

if not drifts:
    raise SystemExit("no artifact inventory drift found; no repair gate is needed")

payload = {
    "authority_sha256": hashlib.sha256(authority_bytes).hexdigest(),
    "disposition": "SUPERSEDE_WITH_VERIFIED_GENERATIONS",
    "expected_inventory_drifts": drifts,
    "release_id": authority.release_id,
    "release_manifest_sha256": authority.release_manifest_sha256,
    "schema_version": 1,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_bytes(json.dumps(payload, indent=1, sort_keys=True).encode("utf-8") + b"\n")

print(f"drifted roots: {len(drifts)}")
for d in drifts:
    print(
        f"  {d['root_id']:24s} {d['recorded_inventory_sha256'][:16]}"
        f" -> {d['observed_inventory_sha256'][:16]}"
    )
    successors = sorted(staged_generations(d["root_id"]) - {d["generation_id"]})
    print(
        f"      drifted generation {d['generation_id'][:16]}"
        f"  staged successor {(successors[0][:16] if successors else 'NONE')}"
    )
print(f"\nwritten {OUT}")
print(f"sha256  {hashlib.sha256(OUT.read_bytes()).hexdigest()}")
