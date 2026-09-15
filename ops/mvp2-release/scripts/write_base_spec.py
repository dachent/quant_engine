"""Write the base decomposition spec, repinned to the current run's artifacts.

The model definition - three proxies excess of BIL, one market model and one
market/size/credit model per series - is carried over verbatim from the
recovered 2026-08-11 spec, which the published decomposition generation
`9bc86c0c` independently corroborates. Only the artifact identities move, the
same repinning every other pin in this release needed.
"""

import json
import sys
from pathlib import Path

RECOVERED = Path(
    r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio"
    r"/source-runs/anl-decomp-current-plan-v7-20260811"
    r"/1fcffb81e1f81e7c8079f35d3c42929daeda05257786bd666f68ef47f2800d0d"
    r"/inputs/governance/base-decomposition-spec.json"
)
BASE_SOURCE = Path(r"C:/Dev/scratch/quant-engine/pl12/base-source")
OUT = Path(r"C:/Dev/scratch/quant-engine/pl12/base-spec/base-decomposition-spec.json")
AS_OF = "2026-09-03"


def main() -> int:
    global BASE_SOURCE, OUT
    if len(sys.argv) > 1:
        BASE_SOURCE = Path(sys.argv[1])
    if len(sys.argv) > 2:
        OUT = Path(sys.argv[2])
    spec = json.loads(RECOVERED.read_text(encoding="utf-8"))
    identities = json.loads(
        (BASE_SOURCE / "identities.json").read_text(encoding="utf-8")
    )

    def repin(definition: dict[str, object], relative: str) -> dict[str, object]:
        identity = identities[relative]
        updated = dict(definition)
        updated["expected_sha256"] = identity["sha256"]
        updated["expected_size_bytes"] = identity["size_bytes"]
        if updated.get("relative_path") != relative:
            raise SystemExit(
                f"relative_path drift: {updated.get('relative_path')} != {relative}"
            )
        return updated

    spec["as_of_date"] = AS_OF
    spec["factor_source"] = repin(spec["factor_source"], "factors.csv")
    spec["target_source"] = repin(spec["target_source"], "targets.csv")
    spec["provenance_sources"] = [
        repin(definition, str(definition["relative_path"]))
        for definition in spec["provenance_sources"]
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print("as_of_date          :", spec["as_of_date"])
    print("staleness limit     :", spec["maximum_factor_staleness_days"])
    print(
        "factors             :",
        [f["factor_id"] for f in spec["factor_source"]["factors"]],
    )
    print("models              :", len(spec["models"]))
    print(
        "factor_source        ->",
        spec["factor_source"]["expected_sha256"][:16],
        spec["factor_source"]["expected_size_bytes"],
    )
    print(
        "target_source        ->",
        spec["target_source"]["expected_sha256"][:16],
        spec["target_source"]["expected_size_bytes"],
    )
    for definition in spec["provenance_sources"]:
        print(
            f"provenance {definition['relative_path']:34s} ->",
            definition["expected_sha256"][:16],
            definition["expected_size_bytes"],
        )
    print("written:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
