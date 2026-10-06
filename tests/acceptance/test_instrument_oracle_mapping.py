"""V15: the oracle does not date an account from NAV rows that carry no admitted mapping.

``date.source_last_observation`` of an account is not derivable when none of its
provider-ledger NAV observations on or before the cutoff is MAPPED (all UNMAPPED, or the
status missing: fail closed).  A MAPPED row dated after the cutoff does not admit it, and
the two derivations apply the same rule: the test compares their actual outputs on one
set of ledger rows (the rule functions) and runs each derivation end to end.
"""

from __future__ import annotations

import datetime as dt
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

from derive_a import sources

DATE = "date.source_last_observation"
CAPITAL = "capital.account.cutoff"


# ---------------------------------------------------------------- derivation A


def _derive_a(tmp_path: Path, statuses: dict[str, str | None], only: str = "acct-ib"):
    """Run A on the synthetic world with ``statuses`` (date -> status) on ``only``."""

    _synthetic_root(tmp_path)
    ledger = json.loads(
        tmp_path.joinpath(*LAYOUT[derive.LEDGER].split("/")).read_bytes()
    )
    for row in ledger["nav_observations"]:
        row["mapping_status"] = "MAPPED"
        if row["account_id"] == only and row["as_of_date"] in statuses:
            status = statuses[row["as_of_date"]]
            if status is None:
                row.pop("mapping_status")
            else:
                row["mapping_status"] = status
    _write(tmp_path, derive.LEDGER, json.dumps(ledger))
    return _run(tmp_path)


IN_WINDOW = ("2026-01-01", "2026-01-05", "2026-01-09")  # acct-ib; cutoff is 2026-01-09
AFTER = "2026-01-11"


def _all(status: str | None, later: str | None = "MAPPED") -> dict[str, str | None]:
    statuses = dict.fromkeys(IN_WINDOW, status)
    statuses[AFTER] = later
    return statuses


def test_a_all_unmapped_rows_in_the_window_withhold_the_date(tmp_path: Path) -> None:
    document = _derive_a(tmp_path, _all("UNMAPPED", later="UNMAPPED"))
    assert (DATE, "acct-ib") not in _index(document)
    assert _reasons(document)[(DATE, "acct-ib")] == derive.UNMAPPED_NAV_REASON
    assert _index(document)[(CAPITAL, "acct-ib")]["value"] == "1320.00"


def test_a_missing_status_fails_closed(tmp_path: Path) -> None:
    document = _derive_a(tmp_path, _all(None, later=None))
    assert (DATE, "acct-ib") not in _index(document)
    assert _reasons(document)[(DATE, "acct-ib")] == derive.UNMAPPED_NAV_REASON


def test_a_mapping_after_the_cutoff_does_not_admit_the_account(tmp_path: Path) -> None:
    """The 2026-01-11 row is MAPPED but dated after the 2026-01-09 cutoff."""

    document = _derive_a(tmp_path, _all("UNMAPPED", later="MAPPED"))
    assert (DATE, "acct-ib") not in _index(document)
    assert _reasons(document)[(DATE, "acct-ib")] == derive.UNMAPPED_NAV_REASON


def test_a_a_mapped_row_inside_the_window_keeps_the_date(tmp_path: Path) -> None:
    statuses = _all("UNMAPPED", later="UNMAPPED")
    statuses["2026-01-05"] = "MAPPED"
    document = _derive_a(tmp_path, statuses)
    assert _index(document)[(DATE, "acct-ib")]["value"] == "2026-01-09"


def test_a_other_accounts_and_metrics_are_unchanged(tmp_path: Path) -> None:
    baseline = _index(_derive_a(tmp_path / "base", {}))
    changed = _index(_derive_a(tmp_path / "changed", _all("UNMAPPED", later="MAPPED")))
    assert set(baseline) - set(changed) == {(DATE, "acct-ib")}
    assert set(changed) <= set(baseline)
    assert all(changed[key] == baseline[key] for key in changed)


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


def _b_rows(*statuses: tuple[str, str | None]) -> list[dict[str, Any]]:
    rows = []
    for day, status in statuses:
        row = nav("account-a", day, "100")
        if status is None:
            row.pop("mapping_status", None)
        else:
            row["mapping_status"] = status
        rows.append(row)
    return rows


def _b_date(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in output["metrics"] if r["metric_id"] == DATE]


WINDOW_B = ("2026-09-15", "2026-09-16")  # B's cutoff is 2026-09-17
AFTER_B = "2026-09-18"


def _b_all(status: str | None, later: str | None) -> list[dict[str, Any]]:
    return _b_rows(*[(day, status) for day in WINDOW_B], (AFTER_B, later))


def test_b_all_unmapped_rows_in_the_window_withhold_the_date(tmp_path: Path) -> None:
    derivation, output = _derive_b(tmp_path, _b_all("UNMAPPED", "UNMAPPED"))
    assert _b_date(output) == []
    reasons = [r for r in output["not_derivable"] if r["metric_id"] == DATE]
    assert [(r["entity"], r["reason"]) for r in reasons] == [
        ("account-a", derivation.UNMAPPED_NAV_REASON)
    ]
    capital = [r for r in output["metrics"] if r["metric_id"] == CAPITAL]
    assert [r["value"] for r in capital] == ["100.00"]


def test_b_a_missing_status_fails_closed(tmp_path: Path) -> None:
    _, output = _derive_b(tmp_path, _b_all(None, None))
    assert _b_date(output) == []


def test_b_a_mapping_after_the_cutoff_does_not_admit_the_account(
    tmp_path: Path,
) -> None:
    _, output = _derive_b(tmp_path, _b_all("UNMAPPED", "MAPPED"))
    assert _b_date(output) == []


def test_b_a_mapped_row_inside_the_window_keeps_the_date(tmp_path: Path) -> None:
    rows = _b_rows(
        ("2026-09-15", "UNMAPPED"), ("2026-09-16", "MAPPED"), (AFTER_B, "UNMAPPED")
    )
    _, output = _derive_b(tmp_path, rows)
    assert [r["value"] for r in _b_date(output)] == ["2026-09-16"]


# ------------------------------------------------- the two rules on the same rows


def _ledger_rows(
    window: str | None, later: str | None, account: str = "account-a"
) -> dict[str, Any]:
    rows = []
    for day, status in (
        ("2026-09-15", window),
        ("2026-09-16", window),
        ("2026-09-18", later),
    ):
        row = nav(account, day, "100")
        if status is None:
            row.pop("mapping_status", None)
        else:
            row["mapping_status"] = status
        rows.append(row)
    return {"nav_observations": rows, "flows": []}


def _a_withheld(document: dict[str, Any]) -> bool:
    parsed = sources.parse_ledger(json.dumps(document).encode(), None)  # type: ignore[arg-type]
    return derive._mapping_withheld(parsed, "account-a", dt.date(2026, 9, 17))


def _b_withheld(document: dict[str, Any]) -> bool:
    derivation = load_derivation_module()
    return "account-a" in derivation.accounts_without_admitted_mapping(document)


def test_a_and_b_agree_on_every_mapping_scenario() -> None:
    expected = {
        ("UNMAPPED", "UNMAPPED"): True,
        ("UNMAPPED", "MAPPED"): True,  # a later mapping admits nothing
        (None, "MAPPED"): True,  # a missing status fails closed
        (None, None): True,
        ("MAPPED", "UNMAPPED"): False,
        ("MAPPED", None): False,
    }
    for (window, later), withheld in expected.items():
        document = _ledger_rows(window, later)
        assert (_a_withheld(document), _b_withheld(document)) == (
            withheld,
            withheld,
        ), (window, later)


def test_the_reason_constants_match() -> None:
    assert derive.UNMAPPED_NAV_REASON == load_derivation_module().UNMAPPED_NAV_REASON
