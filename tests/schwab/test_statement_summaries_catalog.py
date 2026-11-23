"""The Schwab catalog carries statement_cash_summaries only when the spec binds the file."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.schwab import SchwabGenerationError, build_generation

from test_schwab_generation import _spec

AMOUNT_NAMES = (
    "beginning_cash",
    "deposits",
    "dividends_interest",
    "ending_cash",
    "expenses",
    "purchases",
    "sales_redemptions",
    "withdrawals",
)


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _row(account: str, start: str, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": account,
        "deposits_tied": True,
        "period_end": start[:8] + "28",
        "period_start": start,
        "source_artifact_id": f"statements/{account}-{start}.pdf",
        "source_sha256": "a" * 64,
        "tie_status": "TIED",
        "withdrawals_tied": True,
        **{name: _amount("0.00") for name in AMOUNT_NAMES},
    }
    row.update(changes)
    return row


def _bind(spec: Path, rows: list[dict[str, Any]], **tamper: Any) -> None:
    document = json.loads(spec.read_text(encoding="utf-8"))
    root = Path(document["roots"]["source"])
    payload = (json.dumps(rows, indent=1, sort_keys=True) + "\n").encode()
    (root / "statement-cash-summaries.json").write_bytes(payload)
    binding = {
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "relative_path": "statement-cash-summaries.json",
        "root_id": "source",
    }
    binding.update(tamper)
    document["statement_cash_summaries"] = binding
    spec.write_text(json.dumps(document), encoding="utf-8")


def _accounts(tmp_path: Path) -> tuple[Path, list[str]]:
    spec = _spec(tmp_path)
    catalog, _, _ = build_generation(spec)
    return spec, sorted(item["account_id"] for item in catalog["accounts"])  # type: ignore[index]


def test_the_rows_reach_the_catalog_only_when_the_spec_binds_the_file(
    tmp_path: Path,
) -> None:
    spec, accounts = _accounts(tmp_path)
    catalog, _, _ = build_generation(spec)
    assert "statement_cash_summaries" not in catalog
    rows = [_row(accounts[0], "2026-01-01"), _row(accounts[0], "2026-02-01")]
    _bind(spec, rows)
    catalog, _, _ = build_generation(spec)
    assert catalog["statement_cash_summaries"] == rows


def test_a_month_with_no_summary_page_is_named_not_dropped(tmp_path: Path) -> None:
    spec, accounts = _accounts(tmp_path)
    gap = {
        key: value
        for key, value in _row(accounts[0], "2026-01-01").items()
        if key not in AMOUNT_NAMES
    }
    gap.update(
        tie_status="CASH_SUMMARY_ABSENT", deposits_tied=False, withdrawals_tied=False
    )
    _bind(spec, [gap])
    catalog, _, _ = build_generation(spec)
    assert catalog["statement_cash_summaries"][0]["tie_status"] == "CASH_SUMMARY_ABSENT"  # type: ignore[index]


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "account-not-in-the-crosswalk"},
        {"tie_status": "TIED", "deposits_tied": False},
        {"deposits_tied": "yes"},
        {"period_start": "2026-03-01", "period_end": "2026-02-28"},
        {"source_sha256": "xyz"},
    ],
)
def test_a_malformed_row_refuses(tmp_path: Path, change: dict[str, Any]) -> None:
    spec, accounts = _accounts(tmp_path)
    _bind(spec, [_row(accounts[0], "2026-01-01", **change)])
    with pytest.raises(SchwabGenerationError):
        build_generation(spec)


def test_repeats_unsorted_rows_and_a_changed_file_refuse(tmp_path: Path) -> None:
    spec, accounts = _accounts(tmp_path)
    first, second = _row(accounts[0], "2026-01-01"), _row(accounts[0], "2026-02-01")
    _bind(spec, [first, first])
    with pytest.raises(SchwabGenerationError, match="repeats"):
        build_generation(spec)
    _bind(spec, [second, first])
    with pytest.raises(SchwabGenerationError, match="sorted"):
        build_generation(spec)
    _bind(spec, [first], expected_sha256="0" * 64)
    with pytest.raises(SchwabGenerationError, match="hash differs"):
        build_generation(spec)
