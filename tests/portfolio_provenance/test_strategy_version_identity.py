"""Focused enforcement of accepted live-path version identity."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_provenance.py"
TARGET_FIELDS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
)


def _load_provenance() -> Any:
    spec = importlib.util.spec_from_file_location(
        "portfolio_provenance_version_identity", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_targets(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TARGET_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_within_window_restatement_rejects_the_old_accepted_live_version(
    tmp_path: Path,
) -> None:
    provenance = _load_provenance()
    original_rows = [
        {
            "series_id": "Synthetic Strategy",
            "date": "2026-01-02",
            "return": "0.01",
            "evidence_layer": "ACCOUNT_LIVE",
            "return_basis": "NET",
            "return_type": "TOTAL",
            "cost": "",
        },
        {
            "series_id": "Synthetic Strategy",
            "date": "2026-01-03",
            "return": "0.02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return_basis": "NET",
            "return_type": "TOTAL",
            "cost": "",
        },
    ]
    old_version = (
        "accepted-live-path:"
        + hashlib.sha256(
            canonical_json_bytes(
                {"rows": original_rows, "series_id": "Synthetic Strategy"}
            )
        ).hexdigest()
    )
    strategies = [
        {
            "name": "Synthetic Strategy",
            "strategy_id": "synthetic-strategy",
            "version_ref": old_version,
        }
    ]
    version_catalog = {
        "strategies": [
            {
                "live_boundary": {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "observed_end": "2026-01-03",
                    "observed_start": "2026-01-02",
                    "version_ref": old_version,
                },
                "strategy_id": "synthetic-strategy",
                "version_ref": old_version,
            }
        ]
    }
    targets_path = tmp_path / "targets.csv"
    restated_rows = [dict(row) for row in original_rows]
    restated_rows[1]["return"] = "0.03"
    _write_targets(targets_path, restated_rows)

    with pytest.raises(
        provenance.ProvenanceValidationError,
        match="accepted live version differs from canonical target rows",
    ):
        provenance._validate_accepted_live_path_versions(
            targets_path,
            strategies,
            version_catalog,
        )
