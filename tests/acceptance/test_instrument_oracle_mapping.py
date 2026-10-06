"""Batch V15: the oracle does not date an account from unmapped ledger NAV rows.

``date.source_last_observation`` for an account whose provider-ledger NAV observations are
all explicitly UNMAPPED (no strategy mapping admitted; the Alpaca 0acb deprecated account)
is not derivable.  Both derivations change identically.  An account with any MAPPED row,
or whose rows state no mapping at all, keeps its date, and no other metric changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_derive_a import (
    LAYOUT,
    _index,
    _reasons,
    _run,
    _synthetic_root,
    _write,
    derive,
)
from test_derive_b import (
    included_row,
    load_derivation_module,
    nav,
    write_selected_inputs,
)

DATE = "date.source_last_observation"
CAPITAL = "capital.account.cutoff"


def _map_rows(ledger: dict[str, Any], account: str, status: str | None) -> None:
    for row in ledger["nav_observations"]:
        if row["account_id"] == account:
            if status is None:
                row.pop("mapping_status", None)
            else:
                row["mapping_status"] = status


# ---------------------------------------------------------------- derivation A


def _derive_a(tmp_path: Path, status: str | None, only: str = "acct-ib"):
    _synthetic_root(tmp_path)
    ledger = json.loads(tmp_path.joinpath(*LAYOUT[derive.LEDGER].split("/")).read_bytes())
    for row in ledger["nav_observations"]:
        row["mapping_status"] = "MAPPED"
    if status != "MAPPED":
        _map_rows(ledger, only, status)
    _write(tmp_path, derive.LEDGER, json.dumps(ledger))
    return _run(tmp_path)


def test_a_all_unmapped_rows_make_the_source_date_not_derivable(tmp_path: Path) -> None:
    document = _derive_a(tmp_path, "UNMAPPED")
    metrics = _index(document)
    assert (DATE, "acct-ib") not in metrics
    assert _reasons(document)[(DATE, "acct-ib")] == derive.UNMAPPED_NAV_REASON
    assert metrics[(CAPITAL, "acct-ib")]["value"] == "1320.00"


def test_a_other_accounts_and_metrics_are_unchanged(tmp_path: Path) -> None:
    baseline = _index(_derive_a(tmp_path / "base", "MAPPED"))
    changed = _index(_derive_a(tmp_path / "changed", "UNMAPPED"))
    assert set(baseline) - set(changed) == {(DATE, "acct-ib")}
    assert set(changed) <= set(baseline)
    assert all(changed[key] == baseline[key] for key in changed)


def test_a_mapped_or_unstated_mapping_keeps_the_date(tmp_path: Path) -> None:
    for name, status in (("mapped", "MAPPED"), ("unstated", None)):
        metrics = _index(_derive_a(tmp_path / name, status))
        assert metrics[(DATE, "acct-ib")]["value"] == "2026-01-09", name


# ---------------------------------------------------------------- derivation B


def _derive_b(tmp_path: Path, rows: list[dict[str, Any]]):
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {"nav_observations": rows, "flows": [], "capital_anchors": []},
        [included_row("account-a", "strategy-a")],
    )
    return derivation, derivation.derive(tmp_path)


def _b_rows(status: str | None) -> list[dict[str, Any]]:
    rows = [nav("account-a", "2026-09-15", "90"), nav("account-a", "2026-09-16", "100")]
    for row in rows:
        if status is not None:
            row["mapping_status"] = status
    return rows


def _b_date(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in output["metrics"] if r["metric_id"] == DATE]


def test_b_all_unmapped_rows_make_the_source_date_not_derivable(tmp_path: Path) -> None:
    derivation, output = _derive_b(tmp_path, _b_rows("UNMAPPED"))
    assert _b_date(output) == []
    reasons = [r for r in output["not_derivable"] if r["metric_id"] == DATE]
    assert [(r["entity"], r["reason"]) for r in reasons] == [
        ("account-a", derivation.UNMAPPED_NAV_REASON)
    ]
    capital = [r for r in output["metrics"] if r["metric_id"] == CAPITAL]
    assert [r["value"] for r in capital] == ["100.00"]


def test_b_mapped_or_unstated_mapping_keeps_the_date(tmp_path: Path) -> None:
    for name, status in (("mapped", "MAPPED"), ("unstated", None)):
        _, output = _derive_b(tmp_path / name, _b_rows(status))
        assert [r["value"] for r in _b_date(output)] == ["2026-09-16"], name


def test_b_one_mapped_row_admits_the_account(tmp_path: Path) -> None:
    rows = _b_rows("UNMAPPED")
    rows[0]["mapping_status"] = "MAPPED"
    _, output = _derive_b(tmp_path, rows)
    assert [r["value"] for r in _b_date(output)] == ["2026-09-16"]


def test_a_and_b_state_the_same_reason() -> None:
    assert derive.UNMAPPED_NAV_REASON == load_derivation_module().UNMAPPED_NAV_REASON
