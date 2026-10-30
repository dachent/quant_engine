"""Write the governed sleeve disclosure inputs (R2) from the evidence they name.

The inputs file carries no measured value: it names the evidence file whose results the report
cites (parsed from that file at preparation time, pinned by sha256 and size here) and the
retained predecessor generation and JSON pointers a comparison is read from (pinned by the
sha256 of that generation's data file).  Re-run after the evidence or the predecessor changes
and commit the result; the sleeve policy manifest pins this file in turn.

usage: build_disclosure_inputs.py --data-root <QE data root> --out <disclosure-inputs.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PREDECESSOR_RELEASE = "01fbe4d85b04f1879d0b9d5c6e2a33298d951b46af72ab1ab182d80899b0651f"
PREDECESSOR_PANEL = "b216a53e86bbd1cb64aca13f49f4d4c08d7a263623165cc26a24b4bcbb9d412f"
EVIDENCE = "timestamp-delta-result.txt"
ENTRIES = (
    ("p123-ai-factor", "account-1b44c987969b9e47"),
    ("p123-eu", "account-d69701da9143c430"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.out.parent / EVIDENCE
    relative = f"dataPortfolio/control-panel/generations/{PREDECESSOR_PANEL}/data/dashboard_data.json"
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
            "release_id": PREDECESSOR_RELEASE,
            "panel_generation_id": PREDECESSOR_PANEL,
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
    args.out.write_bytes(
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
