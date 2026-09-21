"""Preparation must refuse a pinned contract copy the repository has superseded.

`portfolio_current_dependence` already requires the selected financial input contract to equal
the product contract. Any run reaching the report stage already had to satisfy it. But the
report is the fifth stage, so a stale pin bought four completed stages of ledger, baseline,
attribution and analytics work before anything objected -- and the objection, when it came,
named a dependence source rather than a stale copy.

These tests pin the earlier refusal: same predicate, before anything executes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_attended_preparation.py"
CONTRACT = REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"

sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "portfolio_attended_preparation_currency", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load attended preparation")
preparation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparation)


def _config(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    import hashlib

    return {
        "financial_input_contract": {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    }


def test_the_current_contract_is_accepted(tmp_path: Path) -> None:
    copy = tmp_path / "financial-contract.json"
    copy.write_bytes(CONTRACT.read_bytes())

    preparation._validate_product_contract_currency(_config(copy))


def test_a_superseded_copy_is_refused(tmp_path: Path) -> None:
    """A workbook-bearing contract is exactly what the delivery chain was pinned to."""
    superseded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    superseded["product"]["product_version"] = "1.8.0"
    superseded["product"]["final_artifacts"].append(
        {"role": "WORKBOOK", "acceptance": "the native workbook recalculates"}
    )
    copy = tmp_path / "financial-contract.json"
    copy.write_text(json.dumps(superseded, indent=2), encoding="utf-8")

    with pytest.raises(preparation.AttendedPreparationError) as raised:
        preparation._validate_product_contract_currency(_config(copy))

    message = str(raised.value)
    assert (
        "selected financial input contract differs from the product contract" in message
    )
    # The operator must be told which side is stale, not merely that two things differ.
    assert str(copy) in message


def test_the_refusal_names_both_digests(tmp_path: Path) -> None:
    import hashlib

    superseded = CONTRACT.read_bytes() + b"\n"
    copy = tmp_path / "financial-contract.json"
    copy.write_bytes(superseded)

    with pytest.raises(preparation.AttendedPreparationError) as raised:
        preparation._validate_product_contract_currency(_config(copy))

    message = str(raised.value)
    assert hashlib.sha256(superseded).hexdigest()[:16] in message
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest()[:16] in message


def test_the_refusal_does_not_suggest_relaxing_the_check(tmp_path: Path) -> None:
    copy = tmp_path / "financial-contract.json"
    copy.write_bytes(CONTRACT.read_bytes() + b" ")

    with pytest.raises(preparation.AttendedPreparationError) as raised:
        preparation._validate_product_contract_currency(_config(copy))

    assert "re-admit the source chain" in str(raised.value)
