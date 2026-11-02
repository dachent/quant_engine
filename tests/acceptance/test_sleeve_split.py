"""#624: testing a manual split of one execution between two sleeves, from data.

SYNTHETIC case: two sleeves hold 220 and 130 shares of a symbol that reverse-splits
1-for-8 (account 350 -> 43.75 entitled, 43 whole, 0.75 sold for cash).  The real R2
case (RLYB on 2026-02-06) needs ``QE_AI624_R2_FACTS``, ``QE_AI624_R2_TAG_MAP``,
``QE_AI624_R21_CATALOG`` (the R2.1 ``ibkr-catalog.json``) and ``QE_AI624_R2_IBKR_ROOT``.
"""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from acceptance.validate import sleeve_basis, sleeve_bridge, sleeve_split as sp

D = Decimal
PARENT = "acct-parent"
NEW = "sec-new"
OLD = "sec-old"
DAY = date(2026, 2, 6)
PRICE = D("4.7")
COMMISSION = D("-0.036")
MARKS = {
    "2026-02-06": "4.75",
    "2026-02-09": "4.7",
    "2026-02-10": "5.0",
}


def _trade(
    number: int, day: str, reference: str, quantity: str, price: str, commission: str
) -> sleeve_bridge.FlexTrade:
    return sleeve_bridge.FlexTrade(
        str(number),
        date.fromisoformat(day),
        reference,
        D(commission),
        D(quantity),
        "XYZ",
        D(price),
    )


def trades() -> list[sleeve_bridge.FlexTrade]:
    return [
        _trade(1, "2026-01-26", "1-AlphaModel", "220", "0.7", "-1"),
        _trade(2, "2026-01-30", "2-BetaModel", "130", "0.6", "-1"),
        _trade(3, "2026-02-06", "", "-0.75", "4.7", "-0.036"),
    ]


def tag_map() -> dict[str, Any]:
    def sleeve(name: str, pattern: str) -> dict[str, Any]:
        return {
            "sleeve_id": name,
            "effective_start": "2026-01-01",
            "effective_end": None,
            "tags": [{"match": "startswith", "pattern": pattern}],
        }

    return {
        "parents": [
            {
                "parent_account_id": PARENT,
                "sleeves": [sleeve("s1", "AlphaModel"), sleeve("s2", "BetaModel")],
            }
        ]
    }


def facts(*, residue: bool = True) -> dict[str, Any]:
    third = D(1) / D(35)  # 0.75 * 220/350 = 0.4714..: residual vs 27.5 is 1/35
    residue_rows = (
        [
            {
                "day": "2026-02-10",
                "security_id": NEW,
                "quantities": [
                    {
                        "sleeve_id": "s1",
                        "quantity": str(D("0.0285714285714285714285714")),
                    },
                    {
                        "sleeve_id": "s2",
                        "quantity": str(-D("0.0285714285714285714285714")),
                    },
                ],
            }
        ]
        if residue
        else []
    )
    _ = third
    return {
        "parent_account_id": PARENT,
        "sleeves": [
            {"sleeve_id": "s1", "days": [{"day": "2026-02-05", "exposure": "150"}]},
            {"sleeve_id": "s2", "days": [{"day": "2026-02-05", "exposure": "100"}]},
        ],
        "disclosures": {
            "manual_assignments": [
                {
                    "day": "2026-02-06",
                    "how": "split",
                    "quantity": "-0.75",
                    "security_id": NEW,
                    "sleeves": "s1,s2",
                }
            ],
            "offsetting_residues": residue_rows,
        },
    }


def catalog(*, whole: str = "43", action: bool = True) -> dict[str, Any]:
    def action_row(security: str, quantity: str) -> dict[str, Any]:
        return {
            "account_id": PARENT,
            "kind": "RS",
            "mapping_status": "MAPPED",
            "event_date": "2026-02-05",
            "security_id": security,
            "quantity": quantity,
        }

    return {
        "corporate_actions": (
            [action_row(OLD, "-350"), action_row(NEW, "43.75")] if action else []
        ),
        "positions": [
            {
                "account_id": PARENT,
                "security_id": NEW,
                "symbol": "XYZ",
                "as_of_date": day,
                "mark_price": mark,
                "quantity": whole,
            }
            for day, mark in MARKS.items()
        ],
    }


def _evaluate(**overrides: Any) -> sp.SplitEvidence:
    return sp.evaluate_split(
        overrides.get("facts", facts()),
        overrides.get("trades", trades()),
        tag_map(),
        overrides.get("catalog", catalog()),
        day=DAY,
        symbol="XYZ",
    )


def _near(value: Decimal, expected: Fraction, places: int = 30) -> bool:
    return abs(Fraction(value) - expected) < Fraction(1, 10**places)


def test_the_sleeves_quantities_ratio_and_whole_shares_come_from_the_data() -> None:
    evidence = _evaluate()
    assert evidence.pre_quantity == {"s1": 220, "s2": 130}
    assert evidence.account_old == 350
    assert evidence.entitlement == D("43.75")
    assert evidence.ratio == D("0.125")
    assert evidence.fraction_sold == D("0.75")
    assert evidence.whole_after == 43


def test_only_each_sleeves_own_fractional_entitlement_closes_the_whole_shares() -> None:
    evidence = _evaluate()
    assert evidence.closing_candidates == (sp.SPLIT_ENTITLEMENT,)
    own = evidence.candidates[sp.SPLIT_ENTITLEMENT]
    assert own.shares == {"s1": D("0.5"), "s2": D("0.25")}  # 27.5 and 16.25 entitled
    assert all(r == 0 for r in own.residual_shares.values())
    assert own.max_abs_usd_per_day == 0
    for name, candidate in evidence.candidates.items():
        if name != sp.SPLIT_ENTITLEMENT:
            assert not candidate.closes, name


def test_the_old_builders_pro_rata_split_leaves_the_residue_its_facts_disclosed() -> None:
    evidence = _evaluate()
    pro_rata = evidence.candidates[sp.SPLIT_PRO_RATA_QUANTITY]
    assert _near(pro_rata.residual_shares["s1"], Fraction(1, 35))
    assert _near(pro_rata.residual_shares["s2"], -Fraction(1, 35))
    assert evidence.builder_matches_disclosed_residue
    assert not _evaluate(facts=facts(residue=False)).builder_matches_disclosed_residue


def test_the_usd_per_day_of_a_split_is_the_hand_computed_one() -> None:
    evidence = _evaluate()
    pro_rata = evidence.candidates[sp.SPLIT_PRO_RATA_QUANTITY]
    extra = Fraction(1, 35)  # shares charged to s2 beyond its own fraction
    for day, mark in MARKS.items():
        expected = extra * (Fraction("4.7") - Fraction(mark)) + extra / Fraction(
            3, 4
        ) * Fraction("-0.036")
        assert _near(pro_rata.usd_by_day[date.fromisoformat(day)]["s2"], expected)
        assert _near(pro_rata.usd_by_day[date.fromisoformat(day)]["s1"], -expected)
    assert pro_rata.max_abs_usd_per_day > 0
    # all to one sleeve misses by more than the pro rata split does
    for name in ("ALL_TO_s1", "ALL_TO_s2", sp.SPLIT_PRO_RATA_EXPOSURE):
        assert evidence.candidates[name].total_abs_usd > pro_rata.total_abs_usd


def test_the_days_stop_where_the_facts_close_the_residue() -> None:
    evidence = _evaluate()
    assert set(evidence.candidates[sp.SPLIT_ENTITLEMENT].usd_by_day) == {
        date(2026, 2, 6),
        date(2026, 2, 9),
        date(2026, 2, 10),
    }


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"facts": {**facts(), "disclosures": {"manual_assignments": []}}},
            sp.SPLIT_NOT_DISCLOSED,
        ),
        ({"trades": trades()[:2]}, sp.SPLIT_TRADE_ABSENT),
        (
            {
                "trades": [
                    *trades(),
                    _trade(4, "2026-01-27", "3-AlphaModel", "10", "0.7", "0"),
                ]
            },
            sp.SPLIT_PRE_QUANTITY,
        ),
        ({"catalog": catalog(whole="44")}, sp.SPLIT_WHOLE_CLOSURE),
        ({"catalog": catalog(action=False)}, sp.SPLIT_CORPORATE_ACTION),
    ],
)
def test_data_that_does_not_support_the_split_is_refused(
    change: dict[str, Any], code: str
) -> None:
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _evaluate(**change)
    assert caught.value.code == code


# ------------------------------------------ the rule, derived independently of the facts


def own_fraction_facts() -> dict[str, Any]:
    """Facts as the builder writes them once the corporate-action rule is applied."""

    document = facts(residue=False)
    document["perimeter_account"] = {
        "days": [{"day": day} for day in sorted({*MARKS, "2026-02-05", "2026-02-04"})]
    }
    document["disclosures"]["manual_assignments"][0]["how"] = (
        "corporate_action_fraction"
    )
    document["disclosures"]["corporate_action_fractions"] = [
        {
            "day": "2026-02-06",
            "security_id": NEW,
            "old_security_id": OLD,
            "rule": "OWN_FRACTIONAL_ENTITLEMENT",
            "action": {
                "event_date": "2026-02-05",
                "kind": "RS",
                "old_quantity": "350",
                "new_quantity": "43.75",
                "ratio": "0.125",
            },
            "execution": {"quantity": "-0.75", "price": "4.7", "commission": "-0.036"},
            "closure": {
                "account_position": "43",
                "entitled_total": "43.75",
                "whole_total": "43",
                "fraction_total": "0.75",
                "sold_quantity": "0.75",
            },
            "sleeves": [
                {
                    "sleeve_id": "s1",
                    "held_quantity": "220",
                    "entitlement": "27.5",
                    "whole_shares": "27",
                    "fraction": "0.5",
                    "charged_quantity": "-0.5",
                    "cash_credited": "2.35",
                    "commission": "-0.024",
                },
                {
                    "sleeve_id": "s2",
                    "held_quantity": "130",
                    "entitlement": "16.25",
                    "whole_shares": "16",
                    "fraction": "0.25",
                    "charged_quantity": "-0.25",
                    "cash_credited": "1.175",
                    "commission": "-0.012",
                },
            ],
        }
    ]
    return document


def _check(document: dict[str, Any]) -> list[sleeve_basis.Finding]:
    return sp.check_corporate_action_fractions(document, trades(), tag_map(), catalog())


def _codes(findings: list[sleeve_basis.Finding]) -> set[str]:
    return {f.code for f in findings}


def test_the_rule_is_derived_from_the_data_alone() -> None:
    (found,) = sp.derive_fractions(own_fraction_facts(), trades(), tag_map(), catalog())
    assert (found.day, found.symbol, found.security_id) == (DAY, "XYZ", NEW)
    assert (found.old_quantity, found.new_quantity) == (350, D("43.75"))
    by_sleeve = {s.sleeve_id: s for s in found.sleeves}
    assert by_sleeve["s1"].entitlement == D("27.5") and by_sleeve["s1"].whole == 27
    assert by_sleeve["s2"].entitlement == D("16.25") and by_sleeve["s2"].whole == 16
    assert by_sleeve["s1"].charged == D("-0.5") and by_sleeve["s2"].charged == D(
        "-0.25"
    )
    assert by_sleeve["s1"].cash == D("2.35") and by_sleeve["s2"].cash == D("1.175")
    assert by_sleeve["s1"].commission + by_sleeve["s2"].commission == D("-0.036")


def test_facts_that_apply_the_rule_have_no_finding() -> None:
    assert _check(own_fraction_facts()) == []


def test_facts_that_split_the_fraction_pro_rata_are_refused() -> None:
    document = own_fraction_facts()
    document["disclosures"]["manual_assignments"][0]["how"] = "split"
    del document["disclosures"]["corporate_action_fractions"]
    document["disclosures"]["offsetting_residues"] = [
        {"day": "2026-02-10", "security_id": NEW, "quantities": []}
    ]
    assert _codes(_check(document)) == {
        sp.CA_PRO_RATA_USED,
        sp.CA_NOT_DISCLOSED,
        sp.CA_RESIDUE_LEFT,
    }


def test_a_disclosed_fraction_that_differs_from_the_derivation_is_refused() -> None:
    document = own_fraction_facts()
    row = document["disclosures"]["corporate_action_fractions"][0]["sleeves"][1]
    row["fraction"] = "0.2786"  # the pro rata share, not the sleeve's own
    row["charged_quantity"] = "-0.2786"
    findings = _check(document)
    assert _codes(findings) == {sp.CA_DISCLOSURE_DIFFERS}
    assert {f.where.rsplit(".", 1)[-1] for f in findings} == {
        "fraction",
        "charged_quantity",
    }
    assert all("sleeves[s2]" in f.where for f in findings)


def test_every_published_number_of_the_block_is_compared() -> None:
    document = own_fraction_facts()
    block = document["disclosures"]["corporate_action_fractions"][0]
    block["closure"]["whole_total"] = "42"
    block["action"]["new_quantity"] = "44"
    block["execution"]["price"] = "4.8"
    block["sleeves"][0]["cash_credited"] = "9"
    block["sleeves"][0]["entitlement"] = "28"
    names = {f.where.split("]", 1)[1] for f in _check(document)}
    assert {
        ".closure.whole_total",
        ".action.new_quantity",
        ".execution.price",
        ".sleeves[s1].cash_credited",
        ".sleeves[s1].entitlement",
    } <= names


def test_a_fraction_the_data_do_not_support_is_refused() -> None:
    document = own_fraction_facts()
    unsupported = json.loads(
        json.dumps(document["disclosures"]["corporate_action_fractions"][0])
    )
    unsupported["day"] = "2026-02-09"
    document["disclosures"]["corporate_action_fractions"].append(unsupported)
    assert _codes(_check(document)) == {sp.CA_DISCLOSURE_UNSUPPORTED}


def test_an_ordinary_untagged_sale_is_not_a_corporate_action_fraction() -> None:
    other = [*trades()[:2], _trade(3, "2026-02-06", "", "-0.5", "4.7", "-0.036")]
    assert sp.derive_fractions(own_fraction_facts(), other, tag_map(), catalog()) == []


def test_the_data_that_break_the_closure_stop_the_derivation() -> None: with pytest.raises(sleeve_basis.Refusal) as caught: sp.derive_fractions( own_fraction_facts(), trades(), tag_map(), catalog(whole="44") ) assert caught.value.code == sp.SPLIT_WHOLE_CLOSURE # the sleeves hold more than the surrendered quantity: the pre-split closure fails extra = [*trades(), _trade(9, "2026-01-27", "9-AlphaModel", "10", "0.7", "0")] with pytest.raises(sleeve_basis.Refusal) as caught: sp.derive_fractions(own_fraction_facts(), extra, tag_map(), catalog()) assert caught.value.code == sp.SPLIT_PRE_QUANTITY # with no corporate action in the catalog there is nothing to apply the rule to assert ( sp.derive_fractions( own_fraction_facts(), trades(), tag_map(), catalog(action=False) ) == [] )


# ------------------------------------------------------------------------ real bytes

FACTS = os.environ.get("QE_AI624_R2_FACTS")
TAGS = os.environ.get("QE_AI624_R2_TAG_MAP")
CATALOG = os.environ.get("QE_AI624_R21_CATALOG")
ROOT = os.environ.get("QE_AI624_R2_IBKR_ROOT")
ACCOUNT = os.environ.get("QE_AI624_R2_IBKR_ACCOUNT", "U22596907")
real = pytest.mark.skipif(
    not all(p and Path(p).exists() for p in (FACTS, TAGS, CATALOG, ROOT)),
    reason="the R2 facts, tag map, R2.1 catalog and IBKR root are not all set",
)


R22 = os.environ.get("QE_AI624_R22_FACTS")
R21 = os.environ.get("QE_AI624_R21_FACTS")


@real
def test_the_rebuilt_facts_apply_the_rule_and_the_pro_rata_facts_do_not() -> None:
    assert FACTS and TAGS and CATALOG and ROOT
    if not (R22 and R21 and Path(R22).exists() and Path(R21).exists()):
        pytest.skip("QE_AI624_R22_FACTS and QE_AI624_R21_FACTS are not set")
    tag_map_ = json.loads(Path(TAGS).read_bytes())
    catalog_ = json.loads(Path(CATALOG).read_bytes())
    with ExitStack() as stack:
        paths = sorted(
            Path(ROOT).glob(
                "cutover-recovery/legacy-original-*/AllAccounts_Accounting_XML_2026*.xml"
            )
        ) + sorted(Path(ROOT).glob("source-runs/flex-*/statement.xml"))
        handles = [stack.enter_context(p.open("rb")) for p in paths]
        executions = sleeve_bridge.read_flex_trades(handles, ACCOUNT)
    own = json.loads(Path(R22).read_bytes())
    assert (
        sp.check_corporate_action_fractions(own, executions, tag_map_, catalog_) == []
    )
    derived = sp.derive_fractions(own, executions, tag_map_, catalog_)
    assert [(f.day, f.symbol) for f in derived] == [(date(2026, 2, 6), "RLYB")]
    for path in (R21, FACTS):
        old = json.loads(Path(path).read_bytes())
        codes = {
            f.code
            for f in sp.check_corporate_action_fractions(
                old, executions, tag_map_, catalog_
            )
        }
        assert codes == {sp.CA_PRO_RATA_USED, sp.CA_NOT_DISCLOSED, sp.CA_RESIDUE_LEFT}


@real
def test_the_rlyb_split_of_2026_02_06_on_the_real_bytes(capsys: Any) -> None:
    assert FACTS and TAGS and CATALOG and ROOT
    document = json.loads(Path(FACTS).read_bytes())
    with ExitStack() as stack:
        paths = sorted(
            Path(ROOT).glob(
                "cutover-recovery/legacy-original-*/AllAccounts_Accounting_XML_2026*.xml"
            )
        ) + sorted(Path(ROOT).glob("source-runs/flex-*/statement.xml"))
        handles = [stack.enter_context(p.open("rb")) for p in paths]
        executions = sleeve_bridge.read_flex_trades(handles, ACCOUNT)
    evidence = sp.evaluate_split(
        document,
        executions,
        json.loads(Path(TAGS).read_bytes()),
        json.loads(Path(CATALOG).read_bytes()),
        day=date(2026, 2, 6),
        symbol="RLYB",
    )
    assert evidence.pre_quantity == {"s1": 220, "s2": 130}
    assert (evidence.account_old, evidence.entitlement) == (350, D("43.75"))
    assert evidence.whole_after == 43
    assert evidence.fraction_sold == D("0.75")
    # only each sleeve's own fractional entitlement (27.5 and 16.25 shares) closes
    assert evidence.closing_candidates == (sp.SPLIT_ENTITLEMENT,)
    assert evidence.candidates[sp.SPLIT_ENTITLEMENT].shares == {
        "s1": D("0.5"),
        "s2": D("0.25"),
    }
    # the builder's pro rata split leaves exactly the residue the facts disclose
    assert evidence.builder_matches_disclosed_residue
    pro_rata = evidence.candidates[sp.SPLIT_PRO_RATA_QUANTITY]
    assert f"{pro_rata.max_abs_usd_per_day:.4f}" == "0.1805"
    for name, candidate in evidence.candidates.items():
        print(
            name,
            {k: f"{v:.6f}" for k, v in candidate.residual_shares.items()},
            candidate.closes,
            f"max/day {candidate.max_abs_usd_per_day:.4f}",
            f"total {candidate.total_abs_usd:.4f}",
        )
