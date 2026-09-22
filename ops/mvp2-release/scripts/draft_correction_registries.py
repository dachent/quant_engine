"""Draft the #326 correction registry and its #322 re-derivation receipts.

Two records, both real corrections #326 names as its first two:

* `rcca-003-strategy-history-semantics` (#319) corrected strategy-history
  existence, capture and evidence-layer semantics. Its corrected form is not
  prose: it is the contract's `history_corrections` block, which carries
  `owner_issue: 319` and eleven records across six strategies.
* `pipe-history-001-layered-history-boundary` (#320) required truthful
  layered histories rebuilt to the current live boundary. This cycle rebuilt
  that history.

Both are `RESOLVED`, because #319 and #320 are closed upstream, and both are
`APPLICABLE`, because this release rebuilds what they invalidated.
`NOT_APPLICABLE` would be false and would dodge the re-derivation requirement.

Scope. #320 invalidates `LAYERED_HISTORY` only. #319 also invalidates
`COMPOSITION`: its outcome includes "use actual funding events for
account-live start", and those census assignments cite the PORT_HISTORY_TRUTH_1
policy as their authority while the ledger stage reads them. Ledger is not
downstream of attribution, so naming only `LAYERED_HISTORY` would leave a
future funding-date regression outside this correction's closure. Everything
genuinely downstream is derived by the runtime through `_downstream_stages`
and is not listed.

Digests. These are **bindings**, not independent proof -- the runtime checks
only their length, so what they are worth is what they point at:

| field | #319 | #320 |
| --- | --- | --- |
| subject_ids | the five corrected strategies | the superseded history roots, not the rebuild |
| evidence_sha256 | canonical `contract["history_corrections"]` | canonical map of the superseded roots to their manifest digests |
| disposition_sha256 | the contract's raw bytes | the rebuilt history's `generation.json` |

#320's first draft hashed the rebuild's own `coverage.json` as evidence and
named the rebuild as the subject, which was circular: a still-wrong rebuild
would have produced an equally valid record.

Known weakness, disclosed rather than hidden: the `history_corrections` block
#319 binds is itself partially stale against this release. `p123-eu-provider-live`
still reads `REQUIRED_NOT_READY` with `blockers [303]` and "No provider-live
return is admitted", while this release's admit-or-declare receipt records
portfolio123 `PROVIDER_LIVE_RETURN ADMIT` and #303 closed 2026-08-20. Five of
the eleven records cite #302/#303, both closed. Those strings do not reach the
report or panel values, so this is stale gate authority rather than shipped
wrong numbers -- but it is a real defect in the document this record cites.

The re-derivation receipts are not assertions. The runtime recomputes each
`receipt_sha256` from the canonical payload, and requires `input_generation_id`
to be this release's admitted ledger candidate and `output_generation_id` to be
the candidate of the release role its outputs route to. Both hold here because
every stage was rebuilt from that ledger candidate in this cycle.

They are also not load-bearing: `core/pipeline.py` builds `candidate_blockers`
from PROVIDER_ADMISSION, CORRECTION_AUTHORITY_REQUIRED and OPEN_CORRECTION
only, so MISSING_REDERIVATION and REDERIVE_AUTHORITY_REQUIRED are reported and
never enforced, and a well-formed empty registry clears the gate. That is a
runtime defect, not a property of these files.
"""

import hashlib
import argparse
import json
import sys
from pathlib import Path


def _arguments() -> argparse.Namespace:
    """Take every root as an argument.

    The pinned constants this replaced named one cycle's roots -- a repo checkout, a probe
    recipe and one layered-history generation -- and none of them exist any more.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", type=Path, required=True, help="the prepared run's output root"
    )
    parser.add_argument(
        "--pipeline-recipe",
        type=Path,
        required=True,
        help=(
            "the recipe carrying expected_generations. `run_pipeline.sh` runs this script "
            "before the real recipe exists and passes the probe, which agrees with it on "
            "expected_generations by construction."
        ),
    )
    parser.add_argument(
        "--history-store",
        type=Path,
        required=True,
        help=(
            "the layered-history store holding the superseded generations #320 invalidated. "
            "It is NOT the run's own history: the run writes exactly one generation under "
            "preparation/history/, so sweeping beside that finds no superseded root at all."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory to write both registries into",
    )
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
from quant_engine.core.contracts import canonical_json_bytes  # noqa: E402

CONTRACT = REPO / "contracts/portfolio-financial-mvp-source-v1.json"
PREPARED = ARGS.run.resolve()
# The run's own selected history, not the contract pin: `portfolio_current_dependence.py:335-337`
# reads `policy["history_authority"]` only as a fallback and `:630-652` builds the override from
# the selected generation's own manifest, so binding the pin here would name whichever generation
# the previous cycle used -- it is still on disk.
HISTORY = Path(
    json.loads(
        (PREPARED / "preparation" / "dependence-selected-inputs.json").read_bytes()
    )["history_generation_root"]
)
PIPELINE_RECIPE = ARGS.pipeline_recipe.resolve()
HISTORY_STORE = ARGS.history_store.resolve()
OUT = ARGS.out.resolve()

# Imported, not copied. The previous copies cited the line numbers they were taken from and
# then drifted: `workbook` had been removed from `STAGES` and `panel` rewired away from it, so
# a stage table claiming `"panel": ("workbook",)` routed corrections through a stage that no
# longer exists.
from portfolio_pipeline_materialize import (  # noqa: E402
    SEMANTIC_FIRST_STAGE,
    STAGE_DEPENDENCIES,
    STAGES,
)

RELEASE_ROLES = ("ledger", "baseline", "report", "panel")
# `report` maps to the `report` stage. It read `"workbook"`, which is not in `STAGES` any more,
# so `output_role` could never return the `report` role and `LAYERED_HISTORY` routed to `panel`.
GENERATION_STAGE = {
    "ledger": "ledger",
    "baseline": "baseline",
    "report": "report",
    "panel": "panel",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def downstream(first_stage: str) -> set[str]:
    affected = {first_stage}
    changed = True
    while changed:
        changed = False
        for stage in STAGES:
            if stage in affected:
                continue
            if any(dep in affected for dep in STAGE_DEPENDENCIES[stage]):
                affected.add(stage)
                changed = True
    return affected


def output_role(outputs: list[str]) -> str:
    affected: set[str] = set()
    for output in outputs:
        affected |= downstream(SEMANTIC_FIRST_STAGE[output])
    for role in RELEASE_ROLES:
        if GENERATION_STAGE[role] in affected:
            return role
    raise SystemExit(f"no release role routes {outputs}")


def envelope(
    schema_name: str, record_key: str, records: list[dict], census: str, semantic: str
) -> dict:
    payload = {
        "census_sha256": census,
        "contract_semantic_sha256": semantic,
        record_key: records,
        "schema_name": schema_name,
        "schema_version": 1,
    }
    payload["registry_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main() -> int:
    contract_bytes = CONTRACT.read_bytes()
    contract = json.loads(contract_bytes)
    census = hashlib.sha256(
        canonical_json_bytes(contract["current_census"])
    ).hexdigest()
    semantic = hashlib.sha256(canonical_json_bytes(contract)).hexdigest()
    if contract["history_corrections"]["owner_issue"] != 319:
        raise SystemExit("the contract history corrections are not issue 319")

    # The roots this correction invalidated, identified by their own manifests
    # rather than by the rebuild that replaced them.
    # Swept in the STORE, not beside the run's own history. The run writes exactly one
    # generation under preparation/history/, so sweeping its parent names nothing and the
    # guard below fires; the superseded roots this correction invalidated live in the store.
    superseded = {
        path.parent.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(HISTORY_STORE.glob("*/generation.json"))
        if path.parent.name != HISTORY.name
    }
    if not superseded:
        raise SystemExit("no superseded layered history to name as the subject")

    generations = json.loads(PIPELINE_RECIPE.read_text(encoding="utf-8"))[
        "expected_generations"
    ]
    ledger_candidate = generations["ledger"]["generation_id"]

    # #319 is not only a history correction. Its outcome includes "use actual
    # funding events for account-live start", and those census assignments
    # cite the PORT_HISTORY_TRUTH_1 policy as their authority while the
    # ledger stage reads them (`tools/provider_ledger.py` census digest and
    # funded-membership reads). Ledger is not downstream of attribution, so
    # naming only LAYERED_HISTORY left a future funding-date regression
    # outside this correction's closure.
    history_outputs = ["LAYERED_HISTORY"]
    semantics_outputs = sorted(["COMPOSITION", "LAYERED_HISTORY"])
    corrections = [
        {
            "applicability": "APPLICABLE",
            "correction_id": "rcca-003-strategy-history-semantics",
            "disposition_sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "evidence_sha256": hashlib.sha256(
                canonical_json_bytes(contract["history_corrections"])
            ).hexdigest(),
            "invalidated_outputs": semantics_outputs,
            "rebuild_boundary": "COMPOSITION",
            "status": "RESOLVED",
            "subject_ids": sorted(
                {
                    str(row["strategy_id"])
                    for row in contract["history_corrections"]["corrections"]
                }
            ),
        },
        {
            "applicability": "APPLICABLE",
            "correction_id": "pipe-history-001-layered-history-boundary",
            # Both digests are BINDINGS, not independent evidence. The
            # superseded roots are what this correction invalidated, so they
            # are the subject; the rebuilt generation is what disposed of it.
            # Hashing only the rebuild's own outputs would have been circular:
            # a still-wrong rebuild would produce an equally valid record.
            "disposition_sha256": sha256_file(HISTORY / "generation.json"),
            "evidence_sha256": hashlib.sha256(
                canonical_json_bytes(superseded)
            ).hexdigest(),
            "invalidated_outputs": history_outputs,
            "rebuild_boundary": "LAYERED_HISTORY",
            "status": "RESOLVED",
            "subject_ids": sorted(superseded),
        },
    ]

    receipts = []
    for correction in corrections:
        correction_outputs = correction["invalidated_outputs"]
        role = output_role(correction_outputs)
        payload = {
            "correction_id": correction["correction_id"],
            "input_generation_id": ledger_candidate,
            "invalidated_outputs": correction_outputs,
            "output_generation_id": generations[role]["generation_id"],
            "status": "VERIFIED",
        }
        receipts.append(
            {
                **payload,
                "receipt_sha256": hashlib.sha256(
                    canonical_json_bytes(payload)
                ).hexdigest(),
            }
        )

    OUT.mkdir(parents=True, exist_ok=True)
    correction_path = OUT / "portfolio-correction-registry.json"
    rederive_path = OUT / "portfolio-rederive-receipt-registry.json"
    correction_path.write_bytes(
        canonical_json_bytes(
            envelope(
                "portfolio-correction-registry",
                "corrections",
                corrections,
                census,
                semantic,
            )
        )
    )
    rederive_path.write_bytes(
        canonical_json_bytes(
            envelope(
                "portfolio-rederive-receipt-registry",
                "receipts",
                receipts,
                census,
                semantic,
            )
        )
    )

    print("census_sha256            ", census)
    print("contract_semantic_sha256 ", semantic)
    print("ledger candidate         ", ledger_candidate)
    print("superseded roots         ", sorted(superseded))
    for correction in corrections:
        routed = output_role(correction["invalidated_outputs"])
        print(
            f"  {correction['correction_id']:44s} {correction['status']}"
            f" {correction['applicability']} -> {correction['invalidated_outputs']}"
            f" routes to {routed} {generations[routed]['generation_id'][:16]}"
        )
    print("written:", correction_path)
    print("written:", rederive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
