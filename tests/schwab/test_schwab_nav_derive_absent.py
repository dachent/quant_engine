"""A position sold in full after the day is held at the close of the day but has no row in the
capture.  The roll-back derives its row from the rolled-back quantity and the official close of
the day, flags it, and refuses whenever the row cannot be derived without a guess."""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import schwab_nav_derive as nd
from schwab_rollback import RolledBack

DAY = date(2026, 9, 17)
ACCOUNT = "account-00000000000000a2"
CROSSWALK = {"EXT-A": ACCOUNT}


def _row(symbol: str, qty: str, **extra: Any) -> dict[str, Any]:
    return {
        "dataSymbol": symbol,
        "symbol": {"symbol": symbol, "cusip": "123456789", "ssId": 7},
        "qty": {"qty": qty, "qtyBeforeSplt": qty, "intraDayQuantity": 0},
        "price": {"price": 10.5, "priceDate": "09/17/2026"},
        "marketValue": {"val": 105.0},
        "closePrice": {"val": 10.0},
        "description": "a sibling",
        "costBasis": {"val": 1.0},
        **extra,
    }


def _doc(
    equity: list[dict[str, Any]] | None = None, etf: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    if equity is not None:
        groups.append(
            {"groupName": "Equity", "securityType": 1, "holdingsRows": equity}
        )
    if etf is not None:
        groups.append({"groupName": "ETF", "securityType": 2, "holdingsRows": etf})
    groups.append(
        {
            "groupName": "Cash",
            "securityType": 9,
            "holdingsRows": [{"marketValue": {"val": "10.00"}}],
        }
    )
    return {"accounts": [{"accountId": "EXT-A", "groupedPositions": groups}]}


def _rolled(quantities: dict[str, str]) -> dict[str, RolledBack]:
    return {
        ACCOUNT: RolledBack(
            quantities={k: Decimal(v) for k, v in quantities.items()},
            cash=Decimal("10.00"),
            rows_rolled_back={"Sell": 1},
            informational={},
            straddlers={},
        )
    }


def _closes(tmp_path: Path, table: dict[str, str]) -> Path:
    folder = tmp_path / "closes"
    folder.mkdir()
    for symbol, close in table.items():
        (folder / f"{symbol}_yahoo_daily.csv").write_text(
            f"date,close\n2026-09-17,{close}\n", encoding="utf-8"
        )
    return folder


def _apply(
    doc: dict[str, Any],
    rolled: dict[str, RolledBack],
    closes: Path | None,
    classification: dict[tuple[str, str], tuple[str, int]] | None = None,
):
    return nd.apply_rollback(
        doc,
        CROSSWALK,
        rolled,
        closes_dir=closes,
        day=DAY if closes else None,
        classification=classification,
    )


def test_a_position_sold_in_full_after_the_day_gets_a_flagged_derived_row(
    tmp_path: Path,
) -> None:
    doc = _doc(equity=[_row("AAA", "10")], etf=[_row("CCC", "5")])
    rolled = _rolled({"AAA": "10", "CCC": "5", "ZZZ": "7"})
    out, zeroed, flags = _apply(
        doc,
        rolled,
        _closes(tmp_path, {"ZZZ": "36.28"}),
        {(ACCOUNT, "ZZZ"): ("Equity", 1)},
    )
    assert zeroed == 0
    assert flags == [
        {
            "account_id": ACCOUNT,
            "symbol": "ZZZ",
            "quantity": "7",
            "official_close": "36.28",
            "group": "Equity",
            "group_source": "EARLIER_GOVERNED_CAPTURE_OF_THE_SAME_ACCOUNT",
            "source": nd.ROLLBACK_SOURCE,
        }
    ]
    rows = out["accounts"][0]["groupedPositions"][0]["holdingsRows"]
    derived = next(r for r in rows if r["dataSymbol"] == "ZZZ")
    assert derived["qty"]["qty"] == "7" and derived["symbol"]["symbol"] == "ZZZ"
    assert derived["price"]["priceDate"] == "09/17/2026"
    assert derived["rollbackSynthesized"] == nd.ROLLBACK_SOURCE
    # nothing of the sibling's values or identity is carried over
    # an allow-list: nothing else of the sibling is carried over
    assert set(derived) == {
        "dataSymbol",
        "symbol",
        "qty",
        "price",
        "rollbackSynthesized",
    }
    assert derived["symbol"] == {"symbol": "ZZZ"} and derived["qty"] == {"qty": "7"}
    assert derived["price"] == {"price": 36.28, "priceDate": "09/17/2026"}
    assert (
        doc["accounts"][0]["groupedPositions"][0]["holdingsRows"][0]["dataSymbol"]
        == "AAA"
    )


def test_without_the_closes_the_old_refusal_stands(tmp_path: Path) -> None:
    doc = _doc(equity=[_row("AAA", "10")])
    with pytest.raises(nd.DeriveError, match="absent from the capture"):
        _apply(doc, _rolled({"AAA": "10", "ZZZ": "7"}), None)


def test_a_position_without_an_official_close_of_the_day_is_refused(
    tmp_path: Path,
) -> None:
    doc = _doc(equity=[_row("AAA", "10")])
    closes = _closes(tmp_path, {"AAA": "10"})
    (closes / "ZZZ_yahoo_daily.csv").write_text("date,close\n2026-09-16,9\n")
    with pytest.raises(nd.DeriveError, match="no positive official close"):
        _apply(doc, _rolled({"AAA": "10", "ZZZ": "7"}), closes)
    with pytest.raises(nd.DeriveError, match="no official close file"):
        _apply(doc, _rolled({"AAA": "10", "YYY": "7"}), closes)


def test_a_field_some_rows_of_the_group_lack_is_not_copied(tmp_path: Path) -> None:
    doc = _doc(equity=[_row("AAA", "10"), _row("CCC", "5", optionalOnlyHere=1)])
    out, _z, _flags = _apply(
        doc,
        _rolled({"AAA": "10", "CCC": "5", "ZZZ": "7"}),
        _closes(tmp_path, {"ZZZ": "3"}),
    )
    rows = out["accounts"][0]["groupedPositions"][0]["holdingsRows"]
    assert "optionalOnlyHere" not in next(r for r in rows if r["dataSymbol"] == "ZZZ")


def test_a_group_whose_rows_do_not_all_carry_quantity_price_and_symbol_is_refused(
    tmp_path: Path,
) -> None:
    broken = _row("CCC", "5")
    del broken["price"]
    doc = _doc(equity=[_row("AAA", "10"), broken])
    with pytest.raises(nd.DeriveError, match="shape of its row is ambiguous"):
        _apply(
            doc,
            _rolled({"AAA": "10", "CCC": "5", "ZZZ": "7"}),
            _closes(tmp_path, {"ZZZ": "3"}),
        )


def test_an_account_without_a_position_row_to_copy_is_refused(tmp_path: Path) -> None:
    doc = _doc()
    with pytest.raises(nd.DeriveError, match="no position row"):
        _apply(doc, _rolled({"ZZZ": "7"}), _closes(tmp_path, {"ZZZ": "3"}))


def test_a_rolled_back_quantity_that_is_not_long_is_refused(tmp_path: Path) -> None:
    doc = _doc(equity=[_row("AAA", "10")])
    with pytest.raises(nd.DeriveError, match="not a long position"):
        _apply(
            doc, _rolled({"AAA": "10", "ZZZ": "-2"}), _closes(tmp_path, {"ZZZ": "3"})
        )


def test_a_position_only_partly_sold_is_not_derived(tmp_path: Path) -> None:
    """The capture still lists it: its row is rewritten as before and nothing is derived."""

    doc = _doc(equity=[_row("AAA", "6")])
    out, zeroed, flags = _apply(doc, _rolled({"AAA": "10"}), _closes(tmp_path, {}))
    assert flags == [] and zeroed == 0
    row = out["accounts"][0]["groupedPositions"][0]["holdingsRows"][0]
    assert row["qty"]["qty"] == "10" and "rollbackSynthesized" not in row


def test_an_unchanged_account_keeps_every_byte(tmp_path: Path) -> None:
    doc = _doc(equity=[_row("AAA", "10")])
    before = copy.deepcopy(doc)
    out, _zeroed, flags = _apply(doc, _rolled({"AAA": "10"}), _closes(tmp_path, {}))
    assert flags == [] and out == before


def test_the_derivation_record_names_the_derived_row_and_only_for_that_account(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    (run / "raw/balances.historical").mkdir(parents=True)
    doc = {
        "accounts": [
            {
                "accountId": "EXT-A",
                "groupedPositions": [
                    {
                        "groupName": "ETF",
                        "securityType": 2,
                        "holdingsRows": [_row("BBB", "4")],
                    },
                    {
                        "groupName": "Cash",
                        "securityType": 9,
                        "holdingsRows": [{"marketValue": {"val": "100.50"}}],
                    },
                ],
            },
            {
                "accountId": "EXT-B",
                "groupedPositions": [
                    {
                        "groupName": "ETF",
                        "securityType": 2,
                        "holdingsRows": [_row("BBB", "4")],
                    },
                    {
                        "groupName": "Cash",
                        "securityType": 9,
                        "holdingsRows": [{"marketValue": {"val": "20.00"}}],
                    },
                ],
            },
        ]
    }
    (run / "raw/holdings.v2.json").write_text(json.dumps(doc), encoding="utf-8")
    other = "account-00000000000000b3"
    crosswalk = {"EXT-A": ACCOUNT, "EXT-B": other}

    def history(prior: float, day: float) -> str:
        return json.dumps(
            {
                "histories": {
                    "9/16/2026": {"AccountValue": prior},
                    "9/17/2026": {"AccountValue": day},
                }
            }
        )

    # A held 1 AAA at the close and sold it on 09/18 for 96.00 (cash 100.50 now, 4.50 then):
    # 1 x 95.50 + 4 x 125 + 4.50 on the day, 1 x 90 + 4 x 120 + 4.50 the day before
    (run / "raw/balances.historical" / f"{ACCOUNT}.json").write_text(
        history(574.5, 600.0)
    )
    (run / "raw/balances.historical" / f"{other}.json").write_text(
        history(500.0, 520.0)
    )
    folder = run / "transactions/normalized/wch/transactions"
    folder.mkdir(parents=True)
    sale = {
        "transactionDate": "09/18/2026",
        "effectiveDate": "09/18/2026",
        "action": "Sell",
        "symbol": "AAA",
        "shareQuantity": "1",
        "amount": "$96.00",
    }
    (folder / f"{ACCOUNT}.json").write_text(json.dumps([sale]), encoding="utf-8")
    (folder / f"{other}.json").write_text("[]", encoding="utf-8")
    closes = tmp_path / "closes"
    closes.mkdir()
    for symbol, rows in {
        "AAA": ("90.00", "95.5"),
        "BBB": ("120.00", "125.00"),
    }.items():
        (closes / f"{symbol}_yahoo_daily.csv").write_text(
            f"date,close\n2026-09-16,{rows[0]}\n2026-09-17,{rows[1]}\n"
        )
    record, report = nd.derive(
        run_dir=run,
        crosswalk=crosswalk,
        closes_dir=closes,
        day=DAY,
        prior_day=date(2026, 9, 16),
    )
    assert record[ACCOUNT]["derived_rows_absent_from_capture"] == (
        f"AAA quantity=1 official_close=95.5 source={nd.ROLLBACK_SOURCE}"
    )
    assert "derived_rows_absent_from_capture" not in record[other]
    assert record[ACCOUNT]["official_0917"] == "600.00"


def test_several_position_groups_without_an_earlier_classification_are_refused(
    tmp_path: Path,
) -> None:
    doc = _doc(equity=[_row("AAA", "10")], etf=[_row("CCC", "5")])
    closes = _closes(tmp_path, {"ZZZ": "3"})
    rolled = _rolled({"AAA": "10", "CCC": "5", "ZZZ": "7"})
    with pytest.raises(nd.DeriveError, match="no earlier capture places the symbol"):
        _apply(doc, rolled, closes)
    with pytest.raises(nd.DeriveError, match="no earlier capture places the symbol"):
        _apply(doc, rolled, closes, {(ACCOUNT, "ZZZ"): ("Bonds", 5)})


def test_the_group_an_earlier_capture_names_decides_the_shape(tmp_path: Path) -> None:
    """Equity and ETF rows differ in shape: the row goes into the group the earlier capture names."""

    doc = _doc(equity=[_row("AAA", "10")], etf=[_row("CCC", "5", etfOnly=1)])
    out, _z, flags = _apply(
        doc,
        _rolled({"AAA": "10", "CCC": "5", "ZZZ": "7"}),
        _closes(tmp_path, {"ZZZ": "3"}),
        {(ACCOUNT, "ZZZ"): ("ETF", 2)},
    )
    assert flags[0]["group"] == "ETF"
    etf_rows = out["accounts"][0]["groupedPositions"][1]["holdingsRows"]
    equity_rows = out["accounts"][0]["groupedPositions"][0]["holdingsRows"]
    assert "ZZZ" in [r["dataSymbol"] for r in etf_rows]
    assert "ZZZ" not in [r["dataSymbol"] for r in equity_rows]


def test_the_classification_reader_maps_symbols_to_their_groups() -> None:
    doc = _doc(equity=[_row("AAA", "10")], etf=[_row("CCC", "5")])
    assert nd.holdings_classification(doc, CROSSWALK) == {
        (ACCOUNT, "AAA"): ("Equity", 1),
        (ACCOUNT, "CCC"): ("ETF", 2),
    }


def test_a_close_that_is_not_positive_is_refused(tmp_path: Path) -> None:
    doc = _doc(equity=[_row("AAA", "10")])
    for bad in ("0", "-5"):
        closes = tmp_path / f"c{bad}"
        closes.mkdir()
        (closes / "ZZZ_yahoo_daily.csv").write_text(f"date,close\n2026-09-17,{bad}\n")
        with pytest.raises(nd.DeriveError, match="no positive official close"):
            _apply(doc, _rolled({"AAA": "10", "ZZZ": "7"}), closes)


def test_a_close_file_that_repeats_a_date_is_refused(tmp_path: Path) -> None:
    closes = tmp_path / "closes"
    closes.mkdir()
    (closes / "ZZZ_yahoo_daily.csv").write_text(
        "date,close\n2026-09-17,3\n2026-09-17,4\n"
    )
    with pytest.raises(nd.DeriveError, match="repeats 2026-09-17"):
        nd.read_closes(closes, "ZZZ")


def test_the_classification_reader_refuses_a_symbol_listed_twice_and_an_unknown_account() -> (
    None
):
    twice = _doc(equity=[_row("AAA", "10")], etf=[_row("AAA", "5")])
    with pytest.raises(nd.DeriveError, match="lists AAA twice"):
        nd.holdings_classification(twice, CROSSWALK)
    with pytest.raises(nd.DeriveError, match="outside the crosswalk"):
        nd.holdings_classification(_doc(equity=[_row("AAA", "10")]), {"EXT-X": "x"})


def test_a_derived_row_on_an_account_whose_native_nav_does_not_agree_is_refused(
    tmp_path: Path,
) -> None:
    """The closure against the provider's own balance is what checks a derived row: a stale carry
    (patched) or a waived prior closure leaves nothing to check it, so the record refuses."""

    run = tmp_path / "run"
    (run / "raw/balances.historical").mkdir(parents=True)
    doc = {
        "accounts": [
            {
                "accountId": "EXT-A",
                "groupedPositions": [
                    {
                        "groupName": "ETF",
                        "securityType": 2,
                        "holdingsRows": [_row("BBB", "4")],
                    },
                    {
                        "groupName": "Cash",
                        "securityType": 9,
                        "holdingsRows": [{"marketValue": {"val": "100.50"}}],
                    },
                ],
            }
        ]
    }
    (run / "raw/holdings.v2.json").write_text(json.dumps(doc), encoding="utf-8")

    def history(prior: float, day: float) -> str:
        return json.dumps(
            {
                "histories": {
                    "9/16/2026": {"AccountValue": prior},
                    "9/17/2026": {"AccountValue": day},
                }
            }
        )

    # AAA (1 share, sold on 09/18) is held at the close: official value 600.00, prior 574.50;
    # a native balance that just repeats the day before (574.50 twice) is a stale carry
    (run / "raw/balances.historical" / f"{ACCOUNT}.json").write_text(
        history(574.5, 574.5)
    )
    folder = run / "transactions/normalized/wch/transactions"
    folder.mkdir(parents=True)
    sale = {
        "transactionDate": "09/18/2026",
        "effectiveDate": "09/18/2026",
        "action": "Sell",
        "symbol": "AAA",
        "shareQuantity": "1",
        "amount": "$96.00",
    }
    (folder / f"{ACCOUNT}.json").write_text(json.dumps([sale]), encoding="utf-8")
    closes = tmp_path / "closes"
    closes.mkdir()
    for symbol, pair in {"AAA": ("90.00", "95.5"), "BBB": ("120.00", "125.00")}.items():
        (closes / f"{symbol}_yahoo_daily.csv").write_text(
            f"date,close\n2026-09-16,{pair[0]}\n2026-09-17,{pair[1]}\n"
        )
    with pytest.raises(nd.DeriveError, match="nothing checks them"):
        nd.derive(
            run_dir=run,
            crosswalk={"EXT-A": ACCOUNT},
            closes_dir=closes,
            day=DAY,
            prior_day=date(2026, 9, 16),
        )
