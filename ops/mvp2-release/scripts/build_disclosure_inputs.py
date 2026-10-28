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

PREDECESSOR_RELEASE = "aa40fc6d261e2017b4d62ff03d0ca152b22c82d59c5469a91e427adef59a07df"
PREDECESSOR_PANEL = "85d0e609542c292b31dda87bd1925d9e580d854faf75a46004290abd716d6fbd"
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
            },
        },
    }
    args.out.write_bytes(
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
