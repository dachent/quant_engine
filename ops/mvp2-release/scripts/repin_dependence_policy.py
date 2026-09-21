"""Repin the dependence policy onto artifacts that exist.

The policy named layered history `40e8f1bf` (receipt `ea5ae3cf`) and allocation
generation `7238806c` (manifest `89bdf18e`, catalog `bfd984ef`). None of those
bytes are on this machine: a sha256 scan over 2,458 candidate files across
`dataPortfolio` and `audit` matched none of the three. There is therefore no
run that uses the policy as written, exactly as with the ledger pin
`fba9c941`.

What the policy asserts about *history content* is unchanged by this edit. Its
`live-only.csv`, `live-plus-backtest.csv`, `returns.csv` and `excluded.csv`
hashes are already identical to the rebuilt generation; only `coverage.json`
and `segments.csv` move, and both carry the provider-ledger identity rather
than any return. The accepted return series is byte-for-byte what it was.

The file is stored as the repository's canonical JSON, so it is rewritten with
the same serializer rather than reformatted.
"""

import glob
import hashlib
import json
import sys
from pathlib import Path

sys.path[:0] = glob.glob("packages/*/src")
from quant_engine.core.contracts import canonical_json_bytes  # noqa: E402

POLICY = Path("contracts/portfolio-dependence-policy-v1.json")
HISTORY = Path(
    r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio"
    r"/rollforward/2026-09-10-mvp2-live-only/layered-history"
    r"/df04a9d654f195592e4ba10124a582d35bb393b9bd5b12efa0e95bca26e213cc"
)
ALLOCATION = Path(
    r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio"
    r"/baseline/generations"
    r"/20709bfd5f9a5846879854bf3166c11223a100cc62fcb13fe0b76040f881b542"
)


def digest(path: Path) -> tuple[str, int]:
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def main() -> int:
    global HISTORY, ALLOCATION
    if len(sys.argv) > 1:
        HISTORY = Path(sys.argv[1])
    if len(sys.argv) > 2:
        ALLOCATION = Path(sys.argv[2])
    raw = POLICY.read_bytes()
    policy = json.loads(raw)
    if canonical_json_bytes(policy) != raw:
        raise SystemExit("dependence policy does not round-trip; refusing")

    history = policy["history_authority"]
    previous_outputs = {k: dict(v) for k, v in history["outputs"].items()}
    receipt_sha, _ = digest(HISTORY / "generation.json")
    manifest = json.loads((HISTORY / "generation.json").read_text(encoding="utf-8"))
    history["generation_id"] = manifest["generation_id"]
    history["receipt_sha256"] = receipt_sha
    unchanged, moved = [], []
    for name in sorted(previous_outputs):
        sha, size = digest(HISTORY / name)
        (unchanged if previous_outputs[name]["sha256"] == sha else moved).append(name)
        history["outputs"][name] = {"sha256": sha, "size_bytes": size}

    allocation = policy["allocation_authority"]
    alloc_manifest_sha, _ = digest(ALLOCATION / "generation.json")
    alloc_catalog_sha, _ = digest(ALLOCATION / "portfolio-baseline.json")
    alloc_manifest = json.loads(
        (ALLOCATION / "generation.json").read_text(encoding="utf-8")
    )
    allocation["generation_id"] = alloc_manifest["generation_id"]
    allocation["manifest_sha256"] = alloc_manifest_sha
    allocation["catalog_sha256"] = alloc_catalog_sha
    allocation["as_of_date"] = alloc_manifest["as_of_date"]

    POLICY.write_bytes(canonical_json_bytes(policy))
    print("history generation ->", history["generation_id"][:16])
    print("history receipt    ->", history["receipt_sha256"][:16])
    print("outputs unchanged  :", unchanged)
    print("outputs moved      :", moved)
    print(
        "allocation gen     ->",
        allocation["generation_id"][:16],
        "as_of",
        allocation["as_of_date"],
        "verification",
        alloc_manifest.get("verification"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
