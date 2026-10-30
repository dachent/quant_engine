"""Write the governed sleeve disclosure inputs (R2) from the evidence they name.

The inputs file carries no measured value: it names the evidence file whose results the report
cites (parsed from that file at preparation time, pinned by sha256 and size here) and the
retained predecessor generation and JSON pointers a comparison is read from (pinned by the
sha256 of that generation's data file).  The predecessor is DERIVED from the committed
CURRENT_RELEASE of the data root (its release id and portfolio-panel generation), never typed
in: the run refuses when the release authority is missing or not committed.  Re-run after the
evidence or the committed release changes and commit the result; the sleeve policy manifest
pins this file in turn.  With --check nothing is written and the run refuses (exit 1) when the
file at --out differs from what the committed release derives.

usage: build_disclosure_inputs.py --data-root <QE data root> --out <disclosure-inputs.json> [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "tools")]

import sleeve_disclosures  # noqa: E402

EVIDENCE = "timestamp-delta-result.txt"
ENTRIES = (
    ("p123-ai-factor", "account-1b44c987969b9e47"),
    ("p123-eu", "account-d69701da9143c430"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        predecessor_release, predecessor_panel = (
            sleeve_disclosures.committed_release_panel(args.data_root)
        )
    except sleeve_disclosures.DisclosureError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    evidence = args.out.parent / EVIDENCE
    relative = f"dataPortfolio/control-panel/generations/{predecessor_panel}/data/dashboard_data.json"
    data_file = args.data_root / relative
    document = {
        "schema_version": 1,
        "artifact_id": "sleeve-disclosure-inputs",
        "timestamp_bounds": {
            "evidence": {
                "path": EVIDENCE,
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "size_bytes": evidence.stat().st_size,
            },
            "entries": [
                {"strategy_id": strategy, "account_id": account}
                for strategy, account in ENTRIES
            ],
        },
        "predecessor_comparison": {
            "release_id": predecessor_release,
            "panel_generation_id": predecessor_panel,
            "data_file": {
                "relative_path": relative,
                "sha256": hashlib.sha256(data_file.read_bytes()).hexdigest(),
            },
            "pointers": {
                "strategy_id": "p123-ai-factor",
                "strategy_capital_list": "/invested_capital/current_by_strategy",
                "strategy_capital_field": "invested_capital_usd",
                "aggregate": "/invested_capital/current_aggregate_usd",
                "hwm_current_index": "/hwm_governed_proof/current_index",
            },
        },
    }
    content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != content:
            print(
                "refused: the pinned disclosure inputs differ from the committed "
                f"CURRENT_RELEASE derivation (release {predecessor_release[:8]}, "
                f"panel {predecessor_panel[:8]})",
                file=sys.stderr,
            )
            return 1
        return 0
    args.out.write_bytes(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
