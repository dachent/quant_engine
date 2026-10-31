"""R-1/R-2/R-3/R-6/R-8: the perimeter, margin and timing disclosures from governed bytes.

Every figure below is computed from small synthetic facts and a synthetic ledger; the
tests check the arithmetic by hand and that a build whose sums do not close is refused.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

import sleeve_disclosures as disclosures

PARENT = "account-aaaa"
S1, S2, S3 = f"{PARENT}-s1", f"{PARENT}-s2", f"{PARENT}-s3"
UNALLOCATED = f"{PARENT}-unallocated"
GENERATION = "g" * 64
FIRST, LAST = "2026-01-26", "2026-02-02"


def _day(
    day: str,
    own: str,
    alloc: str = "0",
    *,
    flow_in: str = "0",
    flow_out: str = "0",
) -> dict[str, str]:
    return {
        "day": day,
        "own_pnl": own,
        "alloc_total": alloc,
        "flow_in": flow_in,
        "flow_out": flow_out,
    }


def _facts(*, spoil_closure: bool = False) -> dict[str, Any]:
    # s1: enters 100 on 01-26 with an entry-day difference of -3 (own -3.5, alloc +0.5),
    #     then gains +10 and +5 and exits on 01-28.
    # s2: enters on 01-27 with 200 (entry-day difference -1), gains +4, +6, stays open.
    s1 = [
        _day("2026-01-26", "-3.5", "0.5", flow_in="100"),
        _day("2026-01-27", "10"),
        _day("2026-01-28", "5"),
    ]
    s2 = [
        _day("2026-01-27", "-1", "0", flow_in="200"),
        _day("2026-01-28", "4"),
        _day("2026-01-29", "6"),
    ]
    sleeves_pnl = {
        "2026-01-26": Decimal("-3"),
        "2026-01-27": Decimal("9"),
        "2026-01-28": Decimal("9"),
        "2026-01-29": Decimal("6"),
    }
    # the remainder: 0.25 of parent residual on 01-27 and -0.05 unexplained on 01-28
    remainder = {"2026-01-27": Decimal("0.25"), "2026-01-28": Decimal("-0.05")}
    closure = []
    for day, pnl in sleeves_pnl.items():
        extra = remainder.get(day, Decimal(0))
        closure.append(
            {
                "day": day,
                "sleeves_own_plus_alloc": str(pnl + (1 if spoil_closure else 0)),
                "residual_and_post_exit": str(extra if day == "2026-01-27" else 0),
                "unexplained": str(extra if day == "2026-01-28" else 0),
                "account_pnl": str(pnl + extra),
            }
        )
    return {
        "artifact_id": disclosures.ACCOUNT_FACTS,
        "provider_id": "p",
        "parent_account_id": PARENT,
        "bindings": {"parent_ledger_generation_id": GENERATION},
        "window": {"first_day": FIRST, "last_day": "2026-01-29"},
        "sleeves": [
            {
                "sleeve_id": "s1",
                "account_id": S1,
                "strategy_id": "gone",
                "days": s1,
            },
            {
                "sleeve_id": "s2",
                "account_id": S2,
                "strategy_id": "product",
                "days": s2,
            },
        ],
        "closure": closure,
        "flows": [
            _flow(S1, UNALLOCATED, "2026-01-26", "100"),
            _flow(S2, S1, "2026-01-27", "120"),
            _flow(S2, UNALLOCATED, "2026-01-27", "80"),
        ],
    }


def _flow(account: str, counterparty: str, day: str, amount: str) -> dict[str, str]:
    return {
        "account_id": account,
        "counterparty_account_id": counterparty,
        "class": "INTERNAL_IN",
        "event_date": day,
        "amount": amount,
    }


def _ledger(*, nav_last: str = "1000") -> dict[str, Any]:
    # opening NAV 900 on 2026-01-21; 50 paid in on 01-22; NAV 960 on 01-25 (the day before
    # the first sleeve day) and nav_last on 01-29
    return {
        "capital_anchors": [
            {
                "account_id": PARENT,
                "capital_type": "OPENING_CAPITAL_ANCHOR",
                "generation_id": GENERATION,
                "opening_nav": {"value": "900"},
                "opening_nav_accounting_date": "2026-01-21",
            }
        ],
        "flows": [
            {
                "account_id": PARENT,
                "nav_recognition_date": "2026-01-22",
                "amount": {"value": "50"},
            },
            {
                "account_id": PARENT,
                "nav_recognition_date": "2025-12-29",
                "amount": {"value": "7"},
            },
        ],
        "nav_observations": [
            {"account_id": PARENT, "as_of_date": "2026-01-25", "nav": {"value": "960"}},
            {
                "account_id": PARENT,
                "as_of_date": "2026-01-29",
                "nav": {"value": nav_last},
            },
        ],
    }


def _sources(tmp_path: Path, facts: dict[str, Any], ledger: dict[str, Any]) -> Any:
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    return disclosures.build_sources(
        facts_paths=[facts_path],
        inputs_path=None,
        predecessor_data_root=None,
        ledger_path=ledger_path,
    )


def test_the_account_gain_is_the_sleeve_gains_plus_the_entry_day_differences_and_a_remainder(
    tmp_path: Path,
) -> None:
    # paid in 900 + 50 = 950; NAV 1000 on the last day: account gain 50.
    # sleeve gains from the first-day NAV: s1 +15, s2 +10 = 25; entry-day differences
    # s1 -3, s2 -1 = -4; remainder 50 - 25 + 4 = 29, and independently the days before the
    # first sleeve day (960 - 950 = 10) plus the facts' residual columns (0.25 - 0.05)
    # would be 10.2 -- so this ledger must be refused until it is made consistent.
    with pytest.raises(disclosures.DisclosureError, match="do not sum"):
        _sources(tmp_path, _facts(), _ledger())


def _closing_ledger() -> dict[str, Any]:
    # account gain 31.2 = 25 (sleeves) - 4 (entry days) + 10.2 (10 before the first sleeve
    # day, 0.25 residual, -0.05 unexplained)
    return _ledger(nav_last="981.2")


def test_a_closing_reconciliation_is_published_from_the_ledger_and_the_facts(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    (reconciliation,) = sources["account_reconciliation"]
    assert Decimal(reconciliation["paid_in"]) == Decimal("950")
    assert Decimal(reconciliation["account_gain"]) == Decimal("31.2")
    assert Decimal(reconciliation["sleeve_gain_total"]) == Decimal("25")
    assert Decimal(reconciliation["entry_day_total"]) == Decimal("-4")
    assert Decimal(reconciliation["unattributed"]) == Decimal("10.2")
    assert Decimal(reconciliation["unattributed_pre_sleeve_days"]) == Decimal("10")
    by_sleeve = {s["sleeve_id"]: s for s in reconciliation["sleeves"]}
    assert Decimal(by_sleeve["s1"]["entry_day_difference"]) == Decimal("-3")
    assert Decimal(by_sleeve["s2"]["gain_from_first_day_nav"]) == Decimal("10")


def test_a_ledger_that_does_not_close_against_the_facts_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(disclosures.DisclosureError, match="do not sum"):
        _sources(tmp_path, _facts(), _ledger(nav_last="990"))


def test_facts_whose_daily_closure_differs_from_the_sleeves_are_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(disclosures.DisclosureError, match="daily closure"):
        _sources(tmp_path, _facts(spoil_closure=True), _closing_ledger())


def test_facts_bound_to_another_ledger_generation_are_refused(tmp_path: Path) -> None:
    ledger = _closing_ledger()
    ledger["capital_anchors"][0]["generation_id"] = "h" * 64
    with pytest.raises(disclosures.DisclosureError, match="different ledger"):
        _sources(tmp_path, _facts(), ledger)


def _rows() -> list[dict[str, Any]]:
    def row(account: str, strategy: str, end: str | None, gain: str) -> dict[str, Any]:
        return {
            "account_id": account,
            "strategy_id": strategy,
            "effective_start": "2026-01-26",
            "effective_end": end,
            "end": "2026-01-29" if end is None else end,
            "status": "OK",
            "gain": gain,
        }

    return [
        row(S2, "product", None, "10"),
        row(S1, "gone", "2026-01-28", "15"),
    ]


def _compose(sources: dict[str, Any], rows: list[dict[str, Any]], **kwargs: Any) -> Any:
    return disclosures.compose(
        sources,
        rows=rows,
        invested_capital={
            "current_by_strategy": [
                {"strategy_id": "product", "invested_capital_usd": "200"}
            ],
            "current_aggregate_usd": "1000",
        },
        sleeve_accounts={S1: PARENT, S2: PARENT},
        product_strategies=["product"],
        **kwargs,
    )


def test_the_perimeter_text_states_the_first_day_basis_and_closes_to_the_cent(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    text = next(
        item["value"]
        for item in _compose(sources, _rows())["items"]
        if item["item"] == "Product perimeter"
    )
    assert "gain +15.00 USD in total (s1 +15.00)" in text
    assert "measured from each sleeve's first-day end-of-day NAV" in text
    assert "(s1 -3.00, s2 -1.00; total -4.00)" in text
    assert "2026-01-21..2026-01-29 is +31.20 on 950.00 of external capital" in text
    assert "opening NAV 900.00 plus 50.00 paid in later" in text
    assert "the sleeves sum to +25.00; the +6.20 difference is the entry-day" in text
    assert "(-4.00) and +10.20 not attributed" in text


def test_a_report_row_whose_gain_differs_from_the_facts_stops_the_build(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    rows = _rows()
    rows[1]["gain"] = "16"
    with pytest.raises(
        disclosures.DisclosureError, match="differs from the sleeve facts"
    ):
        _compose(sources, rows)


def test_the_margin_disclosure_splits_each_declared_allocation_by_where_it_came_from(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    sources["margin_funding_events"] = [
        {"sleeve_id": "s2", "day": "2026-01-27", "kind": "IN", "note": "plus margin"}
    ]
    text = next(
        item["value"]
        for item in _compose(sources, _rows())["items"]
        if item["item"] == "Sleeve capital basis"
    )
    assert "2026-01-27 200.00: 120.00 from s1 and 80.00 from no sleeve" in text
    assert "(declared: plus margin)" in text
    # the gross capital above the capital paid into the account is stated
    assert "exceeds" not in text  # 200 is below the 950 paid in


def test_gross_capital_above_the_capital_paid_in_is_stated_with_the_excess(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    result = disclosures.compose(
        sources,
        rows=_rows(),
        invested_capital={
            "current_by_strategy": [
                {"strategy_id": "product", "invested_capital_usd": "1000.50"}
            ],
            "current_aggregate_usd": "5000",
        },
        sleeve_accounts={S1: PARENT, S2: PARENT},
        product_strategies=["product"],
    )
    text = next(
        i["value"] for i in result["items"] if i["item"] == "Sleeve capital basis"
    )
    assert (
        "Gross capital 1,000.50 exceeds all external capital paid into the account "
        "(950.00) by 50.50." in text
    )


def test_the_proceeds_of_a_closing_sleeve_whose_entries_are_margin_funded_are_named(
    tmp_path: Path,
) -> None:
    facts = _facts()
    # s2 also receives 90 on 01-29 from s3, which closes that day and entered on margin
    s3 = [_day("2026-01-26", "0", flow_in="90"), _day("2026-01-29", "0")]
    facts["sleeves"].append(
        {"sleeve_id": "s3", "account_id": S3, "strategy_id": "other", "days": s3}
    )
    facts["flows"] += [
        _flow(S3, UNALLOCATED, "2026-01-26", "90"),
        _flow(S2, S3, "2026-01-29", "90"),
    ]
    sources = disclosures.build_sources(
        facts_paths=[_write(tmp_path, "f.json", facts)],
        inputs_path=None,
        predecessor_data_root=None,
        declared_inputs_path=_write(
            tmp_path,
            "d.json",
            {
                "funding_events": {
                    "s3": {
                        "2026-01-26": {
                            "kind": "IN",
                            "note": "entry, funded by account margin",
                        },
                        "2026-01-29": {"kind": "EXIT", "note": "closed"},
                    }
                }
            },
        ),
    )
    result = disclosures.compose(
        sources,
        rows=_rows(),
        invested_capital={
            "current_by_strategy": [
                {"strategy_id": "product", "invested_capital_usd": "290"}
            ],
            "current_aggregate_usd": "1000",
        },
        sleeve_accounts={S1: PARENT, S2: PARENT, S3: PARENT},
        product_strategies=["product"],
    )
    text = next(
        i["value"] for i in result["items"] if i["item"] == "Sleeve capital basis"
    )
    assert (
        "The 2026-01-29 allocation into s2 (90.00) is the proceeds of s3 (90.00), "
        "whose entries (s3 90.00 on 2026-01-26) are declared account-margin-funded."
    ) in text


def _write(tmp_path: Path, name: str, document: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


EVIDENCE = """=== product IBKR 1 account-aaaa  legs=2 (external=1)
committed TWR            2.453178 %
all legs BOD             2.265307 %   delta -0.187871 pt
external legs BOD only   2.453178 %   delta +0.000000 pt
bounds b_all          lower -2.203870 pt  upper +0.979914 pt
bounds b_ext          lower -1.715383 pt  upper +0.392776 pt
"""


def test_the_timing_evidence_yields_the_all_beginning_of_day_deltas() -> None:
    block = disclosures.parse_timestamp_evidence(EVIDENCE)[("product", PARENT)]
    assert block["all_bod_delta_pt"] == "-0.187871"
    assert block["ext_bod_delta_pt"] == "+0.000000"
    assert (block["b_all_lower_pt"], block["b_all_upper_pt"]) == (
        "-2.203870",
        "+0.979914",
    )


def test_the_timing_text_states_each_flow_independently_and_every_flow_at_beginning_of_day(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "e.txt"
    evidence.write_text(EVIDENCE, encoding="utf-8")
    sources = {
        "facts": [],
        "timestamp_bounds": {
            "evidence": {"path": "e.txt", "sha256": "a" * 64, "size_bytes": 1},
            "entries": [
                {
                    "strategy_id": "product",
                    "account_id": PARENT,
                    **disclosures.parse_timestamp_evidence(EVIDENCE)[
                        ("product", PARENT)
                    ],
                }
            ],
        },
    }
    row = {
        "account_id": PARENT,
        "strategy_id": "product",
        "end": "2026-01-29",
        "observation_linked_return": "0.02453178",
    }
    text = next(
        item["value"]
        for item in disclosures.compose(
            sources,
            rows=[row],
            invested_capital=None,
            sleeve_accounts={},
            product_strategies=[],
        )["items"]
        if item["item"] == "Flow timing bounds"
    )
    assert (
        "placing each flow at the beginning or the end of its day, independently, moves "
        "it by between -2.20 and +0.98 points" in text
    )
    assert "placing every flow at beginning of day moves it by -0.19 points" in text


def test_the_previous_release_is_labelled_with_its_release_and_panel_generation(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    sources["predecessor"] = {
        "status": "RESOLVED",
        "release_id": "aa40fc6d" + "0" * 56,
        "panel_generation_id": "85d0e609" + "0" * 56,
        "values": {
            "strategy_id": "product",
            "strategy_capital_usd": "250",
            "aggregate_capital_usd": "800",
        },
    }
    text = next(
        i["value"]
        for i in _compose(sources, _rows())["items"]
        if i["item"] == "Sleeve capital basis"
    )
    assert (
        "Previous release aa40fc6d (panel generation 85d0e609): 250.00 for product "
        "(-50.00, -20.0%) and 800.00 in total (+200.00)." in text
    )


def test_the_hwm_index_move_is_stated_only_from_the_predecessor_and_the_report(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    sources["predecessor"] = {
        "status": "RESOLVED",
        "release_id": "r" * 64,
        "panel_generation_id": "p" * 64,
        "values": {
            "strategy_id": "product",
            "strategy_capital_usd": "250",
            "aggregate_capital_usd": "800",
            "hwm_current_index": "100.0000",
        },
    }
    rows = _rows()
    shown = next(
        i["value"]
        for i in _compose(sources, rows, hwm_current_index="102.0000")["items"]
        if i["item"] == "HWM member series"
    )
    assert (
        "The index is 102.0000 against 100.0000 in the previous release (+2.00, +2.00%)"
        in shown
    )
    # without the report's index, or without a resolved predecessor, the move is left unattributed
    for kwargs in ({}, {"hwm_current_index": None}):
        silent = next(
            i["value"]
            for i in _compose(sources, rows, **kwargs)["items"]
            if i["item"] == "HWM member series"
        )
        assert "The index is" not in silent and "not attributed" in silent


def test_a_product_perimeter_without_a_reconciliation_says_so_in_the_report(
    tmp_path: Path,
) -> None:
    """No reconciliation in the sources: the omission is named, never silent."""

    sources = _sources(tmp_path, _facts(), _closing_ledger())
    sources["account_reconciliation"] = []
    result = _compose(sources, _rows())
    assert {
        "entry": f"perimeter reconciliation of {PARENT}",
        "reason": "ACCOUNT_RECONCILIATION_NOT_IN_SOURCES",
    } in result["omitted"]


def test_a_withdrawal_after_the_anchor_does_not_lower_the_capital_paid_in(
    tmp_path: Path,
) -> None:
    """Codex: net flows are not capital paid in; withdrawals are kept apart."""

    ledger = _ledger(nav_last="981.2")
    ledger["flows"].append(
        {
            "account_id": PARENT,
            "nav_recognition_date": "2026-01-24",
            "amount": {"value": "-20"},
        }
    )
    # the NAV the closing ledger needs rises by the 20 that left
    ledger["nav_observations"][0]["nav"]["value"] = "940"
    ledger["nav_observations"][1]["nav"]["value"] = "961.2"
    (reconciliation,) = _sources(tmp_path, _facts(), ledger)["account_reconciliation"]
    assert Decimal(reconciliation["paid_in"]) == Decimal("950")
    assert Decimal(reconciliation["withdrawn_after_anchor"]) == Decimal("20")
    assert Decimal(reconciliation["account_gain"]) == Decimal("31.2")


def test_every_parent_account_of_a_product_strategy_is_reconciled_or_named(
    tmp_path: Path,
) -> None:
    """Codex: sleeves under two parents are not decided by an arbitrary pick."""

    sources = _sources(tmp_path, _facts(), _closing_ledger())
    other_parent, other_sleeve = "account-bbbb", "account-bbbb-s9"
    rows = _rows() + [
        {
            "account_id": other_sleeve,
            "strategy_id": "product",
            "effective_start": "2026-01-27",
            "effective_end": None,
            "end": "2026-01-29",
            "status": "OK",
            "gain": "1",
        }
    ]
    result = disclosures.compose(
        sources,
        rows=rows,
        invested_capital={
            "current_by_strategy": [
                {"strategy_id": "product", "invested_capital_usd": "5000"}
            ],
            "current_aggregate_usd": "9000",
        },
        sleeve_accounts={S1: PARENT, S2: PARENT, other_sleeve: other_parent},
        product_strategies=["product"],
    )
    assert {
        "entry": f"capital paid into {other_parent}",
        "reason": "ACCOUNT_RECONCILIATION_NOT_IN_SOURCES",
    } in result["omitted"]
    text = next(
        i["value"] for i in result["items"] if i["item"] == "Sleeve capital basis"
    )
    assert "exceeds all external capital" not in text


def test_the_printed_remainder_is_the_sources_unattributed_amount_rounded_once() -> (
    None
):
    reconciliation = {
        "sleeves": [
            {
                "sleeve_id": "s1",
                "account_id": "p",
                "gain_from_first_day_nav": "2677.440211142",
                "entry_day_difference": "-1263.557097",
            }
        ],
        "entry_day_total": "-1263.557097",
        "account_gain": "1441.075514128",
        "sleeve_gain_total": "2677.440211142",
        "unattributed": "27.192399999999999999999993",
        "paid_in_after_anchor": "0",
        "paid_in": "100",
        "opening_nav": "100",
        "anchor_day": "2026-01-01",
        "last_day": "2026-02-01",
    }
    text = disclosures._perimeter_reconciliation(
        reconciliation, [{"account_id": "p", "gain": "2677.440211142"}]
    )
    # The shown figures alone would give +27.20; the source says 27.1924.
    assert "and +27.19 not attributed" in text
    assert "+27.20" not in text
