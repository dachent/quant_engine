"""Aggregates computed from derived NAVs carry the same label the account rows carry.

Synthetic payloads only. The strategy current value, the allocation total and the capital totals are
sums over member accounts; they say so (counts and day from the payload) and nothing is hardcoded.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_panel_render_audit_543 import _allocation_payload, html, run, text
from test_shell_current_capital import _command_payload, _run

DAY = "2026-09-17"


def _basis() -> dict[str, Any]:
    return {
        "basis": "DERIVED_FROM_OFFICIAL_CLOSES",
        "derivation_record_sha256": "d" * 64,
        "finality_evidence_sha256": "e" * 64,
        "official_closes_manifest_sha256": "c" * 64,
        "provider_finality_guarantee": False,
    }


def _member(account: str, day: str = DAY) -> dict[str, Any]:
    return {"account_id": account, "as_of_date": day, "status": "AVAILABLE"}


def _custody(derived_tt_members: int = 1) -> dict[str, Any]:
    tt = [_member(f"tt{i}") for i in range(derived_tt_members)] + [_member("native")]
    return {
        "strategies": [
            {"strategy_id": "tt-id", "members": tt},
            {"strategy_id": "eu-id", "members": [_member("eu0")]},
        ],
        "systematic": {"components": ["tt-id", "eu-id"]},
        "retained_excluded": {"components": []},
        "reserve": {"members": [_member("res0")]},
    }


def _summaries(derived: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "account_id": account,
            "as_of_date": DAY,
            "role": "SYSTEMATIC",
            **({"value_basis": _basis()} if account in derived else {}),
        }
        for account in ("tt0", "tt1", "native", "eu0", "res0")
    ]


def _allocation(tmp_path: Path, *, derived: list[str], tt_derived: int = 1) -> str:
    payload = _allocation_payload(
        custody_current_capital=_custody(tt_derived),
        account_summary=_summaries(derived),
    )
    return html(run(tmp_path, payload, "renderAllocationTable"), "#allocation-table")


def _rows(table: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r)).strip()
        for r in table.split("<tr>")
    ]


def test_a_strategy_current_value_with_a_derived_member_says_so_with_its_counts(
    tmp_path: Path,
) -> None:
    table = _allocation(tmp_path, derived=["tt0"])
    tt = next(row for row in _rows(table) if row.startswith("TT"))
    eu = next(row for row in _rows(table) if row.startswith("EU"))
    assert (
        "Partly derived from official closes as of Sep 17, 2026 (1 of 2 accounts)" in tt
    )
    assert "derived" not in eu


def test_every_member_derived_drops_the_word_partly(tmp_path: Path) -> None:
    table = _allocation(tmp_path, derived=["tt0", "native"])
    tt = next(row for row in _rows(table) if row.startswith("TT"))
    assert "Derived from official closes as of Sep 17, 2026 (2 of 2 accounts)" in tt
    assert "Partly" not in tt


def test_the_counts_follow_the_payload(tmp_path: Path) -> None:
    table = _allocation(tmp_path, derived=["tt0", "tt1"], tt_derived=2)
    tt = next(row for row in _rows(table) if row.startswith("TT"))
    assert "(2 of 3 accounts)" in tt


def test_the_allocation_total_names_the_derived_members_of_the_rows_shown(
    tmp_path: Path,
) -> None:
    table = _allocation(tmp_path, derived=["tt0"])
    total = next(row for row in _rows(table) if row.startswith("Total"))
    assert (
        "Partly derived from official closes as of Sep 17, 2026 (1 of 3 accounts)"
        in total
    )


def test_nothing_is_labelled_without_a_derived_member(tmp_path: Path) -> None:
    table = _allocation(tmp_path, derived=[])
    assert "derived from official closes" not in table.lower()


def test_a_derived_account_on_another_day_than_the_member_is_not_counted(
    tmp_path: Path,
) -> None:
    payload = _allocation_payload(
        custody_current_capital=_custody(),
        account_summary=[
            {**s, "as_of_date": "2026-09-10"} for s in _summaries(["tt0"])
        ],
    )
    table = html(run(tmp_path, payload, "renderAllocationTable"), "#allocation-table")
    assert "derived from official closes" not in table.lower()


def test_the_capital_totals_name_their_derived_components(tmp_path: Path) -> None:
    payload = _command_payload(
        systematic="55", reserve="35", retained="10", total="100"
    )
    payload["custody_current_capital"] = _custody()
    payload["account_summary"] = _summaries(["tt0"])
    dom = _run(tmp_path, payload, "renderCommand")
    kpis = re.sub(r"<[^>]+>", " ", dom["#command-kpis"]["innerHTML"])
    ring = re.sub(r"<[^>]+>", " ", dom["#capital-ring"]["innerHTML"])
    assert (
        "Partly derived from official closes as of Sep 17, 2026 (1 of 3 accounts)"
        in kpis
    )
    assert (
        "Partly derived from official closes as of Sep 17, 2026 (1 of 4 accounts)"
        in kpis
    )
    assert (
        "Partly derived from official closes as of Sep 17, 2026 (1 of 3 accounts)"
        in ring
    )
    plain = _command_payload(systematic="55", reserve="35", retained="10", total="100")
    plain["custody_current_capital"] = _custody()
    plain["account_summary"] = _summaries([])
    quiet = _run(tmp_path, plain, "renderCommand")
    assert (
        "derived from official closes"
        not in quiet["#command-kpis"]["innerHTML"].lower()
    )
    assert (
        "derived from official closes"
        not in quiet["#capital-ring"]["innerHTML"].lower()
    )
    assert text  # shared helper import kept for parity with the allocation tests


def test_the_allocation_total_ignores_members_of_rows_not_in_the_sum(
    tmp_path: Path,
) -> None:
    """A derived member of a not-ready strategy must not label a total that excludes it."""

    payload = _allocation_payload(
        custody_current_capital=_custody(),
        account_summary=_summaries(["eu0"]),  # the derived member belongs to EU
    )
    # EU has no current value (not ready): it is left out of the total
    payload["strategies"] = [
        row for row in payload["strategies"] if row["strategy_name"] != "EU"
    ]
    table = html(run(tmp_path, payload, "renderAllocationTable"), "#allocation-table")
    total = next(row for row in _rows(table) if row.startswith("Total"))
    assert "derived from official closes" not in total.lower()
    assert "not ready" in total


def test_every_capital_kpi_surface_carries_the_aggregate_label(tmp_path: Path) -> None:
    payload = _command_payload(
        systematic="55", reserve="35", retained="10", total="100"
    )
    payload["custody_current_capital"] = _custody()
    payload["account_summary"] = _summaries(["tt0", "res0"])
    label = "derived from official closes as of sep 17, 2026"
    capital = _run(tmp_path, payload, "renderCapitalKpis")
    assert (
        label in re.sub(r"<[^>]+>", " ", capital["#capital-kpis"]["innerHTML"]).lower()
    )
    # the lineage-mismatch branch of the command view keeps the systematic label
    mismatch = {**payload, "meta": {"performance_lineage": {"status": "MISMATCH"}}}
    command = _run(tmp_path, mismatch, "renderCommand")
    kpis = re.sub(r"<[^>]+>", " ", command["#command-kpis"]["innerHTML"]).lower()
    assert kpis.count(label) >= 1
    # the reserve KPI is labelled when every reserve member is derived
    normal = _run(tmp_path, payload, "renderCommand")
    reserve = re.sub(r"<[^>]+>", " ", normal["#command-kpis"]["innerHTML"]).lower()
    assert "(1 of 1 account)" in reserve
