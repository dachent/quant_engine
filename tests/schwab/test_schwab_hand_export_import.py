"""Governed importer for the owner's hand exports of Schwab data (F2 item 37).

Synthetic fixtures only: invented accounts, symbols and amounts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

import schwab_hand_export_import as imp

TX_CSV = (
    '"Transactions  for account XXXX123 as of 11/20/2026 09:00:00 AM ET"\n'
    '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
    '"11/18/2026","Buy","AAA","ALPHA FUND","10","$5.00","","-$50.00"\n'
    '"11/16/2026","Qualified Dividend","AAA","ALPHA FUND","","","","$1.25"\n'
    '"11/02/2026","Buy","BBB","BETA ETF","3","$10.00","","-$30.00"\n'
    '"Transactions Total","","","","","","","-$78.75"\n'
)
AVH_CSV = (
    '"Date","Amount"\n'
    '"11/19/2026","$1,234.50"\n'
    '"11/18/2026","$1,200.00"\n'
    '"11/20/2026","$1,300.00"\n'
)
POSITIONS = """CAPTURED_LOCAL: 2026-11-20T09:12:00.123456-06:00
Account positions
Equities
AAA
ALPHA FUND
10
$5.50
$55.00
BBB
BETA ETF
3
$11.00
$33.00
Cash & Money Market
SWVXX
SCHWAB VALUE ADVANTAGE MONEY
1,146.50
$1.00
$1,146.50
Positions Total
"""


def _retained() -> list[dict[str, object]]:
    return [
        {
            "action": "Buy",
            "amount": "-$30.00",
            "description": "BETA ETF",
            "executionPrice": "$10.00",
            "feesAndCommission": "",
            "shareQuantity": "3",
            "symbol": "BBB",
            "transactionDate": "11/02/2026",
        }
    ]


def test_transactions_csv_rows_are_marked_csv_derived_and_totals_are_dropped() -> None:
    rows = imp.parse_transactions_csv(TX_CSV)
    assert [r["transactionDate"] for r in rows] == [
        "11/18/2026",
        "11/16/2026",
        "11/02/2026",
    ]
    assert all(r["csvDerivedRow"] is True for r in rows)
    assert rows[0]["symbol"] == "AAA" and rows[0]["amount"] == "-$50.00"


def test_merge_keeps_retained_bytes_and_adds_only_the_new_rows_newest_first() -> None:
    rows = imp.parse_transactions_csv(TX_CSV)
    merged, delta = imp.merge_transactions(
        _retained(), rows, capture_day=date(2026, 11, 20)
    )
    assert len(delta) == 2
    assert [r["transactionDate"] for r in merged] == [
        "11/18/2026",
        "11/16/2026",
        "11/02/2026",
    ]
    assert merged[-1] == _retained()[0]  # the retained row is untouched, no csv flag


def test_a_retained_row_missing_from_the_csv_refuses() -> None:
    rows = [r for r in imp.parse_transactions_csv(TX_CSV) if r["symbol"] != "BBB"]
    with pytest.raises(imp.HandExportError, match="retained rows missing"):
        imp.merge_transactions(_retained(), rows, capture_day=date(2026, 11, 20))


def test_a_new_row_older_than_the_newest_retained_row_refuses() -> None:
    retained = _retained()
    retained[0]["transactionDate"] = "11/17/2026"
    # the retained row is in the CSV (11/17) but a new row (11/16) is older than it
    rows = imp.parse_transactions_csv(TX_CSV.replace('"11/02/2026"', '"11/17/2026"'))
    with pytest.raises(imp.HandExportError, match="older than the retained history"):
        imp.merge_transactions(retained, rows, capture_day=date(2026, 11, 20))


def test_rows_after_the_capture_day_are_dropped() -> None:
    rows = imp.parse_transactions_csv(
        TX_CSV.replace('"11/18/2026","Buy"', '"11/21/2026","Buy"')
    )
    merged, delta = imp.merge_transactions(
        _retained(), rows, capture_day=date(2026, 11, 20)
    )
    assert [r["transactionDate"] for r in delta] == ["11/16/2026"]
    assert len(merged) == 2


def test_nav_merge_adds_new_days_and_reports_conflicts_without_applying_them() -> None:
    avh = imp.parse_balances_csv(AVH_CSV)
    assert avh[date(2026, 11, 19)] == Decimal("1234.50")
    retained = {"2026-11-18": "1199.00"}
    series, conflicts = imp.merge_nav(retained, avh, cutoff=date(2026, 11, 19))
    assert series == {"2026-11-18": "1199.00", "2026-11-19": "1234.5"}
    assert conflicts == [
        ("2026-11-18", "1199.00", "1200.00")
    ]  # reported, kept retained
    series2, _ = imp.merge_nav(
        retained, avh, cutoff=date(2026, 11, 19), apply_conflicts=True
    )
    assert series2["2026-11-18"] == "1200"
    assert "2026-11-20" not in series  # beyond the cutoff


def test_positions_page_parses_rows_and_the_capture_stamp() -> None:
    captured, rows, sweep = imp.parse_positions_page(POSITIONS)
    assert captured == "2026-11-20T09:12:00.123456-06:00"
    assert [(r["symbol"], r["quantity"], r["prior_close"]) for r in rows] == [
        ("AAA", Decimal("10"), Decimal("5.50")),
        ("BBB", Decimal("3"), Decimal("11.00")),
    ]
    assert sweep == Decimal("1146.50")


def test_closure_to_the_cent_passes_and_a_difference_refuses() -> None:
    _, rows, sweep = imp.parse_positions_page(POSITIONS)
    # 55.00 + 33.00 + 1146.50 = 1234.50
    assert imp.check_closure("123", rows, sweep, Decimal("1234.50")) == Decimal("0.00")
    with pytest.raises(imp.HandExportError, match="do not close"):
        imp.check_closure("123", rows, sweep, Decimal("1234.51"))


def test_import_writes_create_only_outputs_and_the_hand_export_label(
    tmp_path: Path,
) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "acct_XXX123_Transactions_20261120-090000.csv").write_text(TX_CSV)
    (exports / "acct_XXXX123_Balances_20261120-090000.CSV").write_text(AVH_CSV)
    (exports / "schwab-positions-123.txt").write_text(POSITIONS)
    retained = tmp_path / "retained"
    (retained / "transactions").mkdir(parents=True)
    (retained / "transactions" / "123.json").write_text(json.dumps(_retained()))
    (retained / "nav").mkdir()
    (retained / "nav" / "123.csv").write_text(
        "account_id,account_name,date,account_value\n123,123,2026-11-18,1200.00\n"
    )
    out = tmp_path / "out"
    result = imp.import_exports(
        exports_dir=exports,
        retained_dir=retained,
        out_dir=out,
        accounts=["123"],
        cutoff=date(2026, 11, 19),
        capture_day=date(2026, 11, 20),
    )
    label = json.loads((out / "HAND_EXPORT.json").read_text())
    assert label["provenance"] == "HAND_EXPORT"
    assert label["release_eligible"] is False
    assert label["replay_input"] == "HAND_REBUILT_NOT_A_PROVIDER_RESPONSE"
    assert set(label["inputs"]) == {
        "acct_XXX123_Transactions_20261120-090000.csv",
        "acct_XXXX123_Balances_20261120-090000.CSV",
        "schwab-positions-123.txt",
    }
    assert result["accounts"]["123"]["new_transactions"] == 2
    assert result["accounts"]["123"]["closure_difference"] == "0.00"
    merged = json.loads((out / "transactions" / "123.json").read_text())
    assert len(merged) == 3
    with pytest.raises(imp.HandExportError, match="already exists"):
        imp.import_exports(
            exports_dir=exports,
            retained_dir=retained,
            out_dir=out,
            accounts=["123"],
            cutoff=date(2026, 11, 19),
            capture_day=date(2026, 11, 20),
        )


def _world(tmp_path: Path, *, avh: str = AVH_CSV, tx: str = TX_CSV) -> dict[str, Path]:
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "acct_XXX123_Transactions_20261120-090000.csv").write_text(tx)
    (exports / "acct_XXXX123_Balances_20261120-090000.CSV").write_text(avh)
    (exports / "schwab-positions-123.txt").write_text(POSITIONS)
    retained = tmp_path / "retained"
    (retained / "transactions").mkdir(parents=True)
    (retained / "transactions" / "123.json").write_text(json.dumps(_retained()))
    (retained / "nav").mkdir()
    (retained / "nav" / "123.csv").write_text(
        "account_id,account_name,date,account_value\n123,123,2026-11-18,1200.00\n"
    )
    return {"exports": exports, "retained": retained, "out": tmp_path / "out"}


def _run(w: dict[str, Path], **over: object) -> dict[str, object]:
    args: dict[str, object] = {
        "exports_dir": w["exports"],
        "retained_dir": w["retained"],
        "out_dir": w["out"],
        "accounts": ["123"],
        "cutoff": date(2026, 11, 19),
        "capture_day": date(2026, 11, 20),
    }
    args.update(over)
    return imp.import_exports(**args)  # type: ignore[arg-type]


def test_a_balances_export_without_the_cutoff_day_refuses_and_writes_nothing(
    tmp_path: Path,
) -> None:
    w = _world(tmp_path, avh='"Date","Amount"\n"11/18/2026","$1,200.00"\n')
    with pytest.raises(imp.HandExportError, match="no value on 2026-11-19"):
        _run(w)
    assert not w["out"].exists()


def test_an_existing_output_file_refuses_before_any_output_is_written(
    tmp_path: Path,
) -> None:
    w = _world(tmp_path)
    (w["out"] / "nav").mkdir(parents=True)
    (w["out"] / "nav" / "123.csv").write_text("occupied")
    with pytest.raises(imp.HandExportError, match="already exists"):
        _run(w)
    assert not (w["out"] / "transactions" / "123.json").exists()
    assert not (w["out"] / "HAND_EXPORT.json").exists()


def test_the_label_hashes_the_retained_inputs_too(tmp_path: Path) -> None:
    w = _world(tmp_path)
    _run(w)
    label = json.loads((w["out"] / "HAND_EXPORT.json").read_text())
    assert set(label["retained_inputs"]) == {"transactions/123.json", "nav/123.csv"}


def test_a_csv_that_is_not_utf8_refuses(tmp_path: Path) -> None:
    w = _world(tmp_path)
    (w["exports"] / "acct_XXX123_Transactions_20261120-090000.csv").write_bytes(
        TX_CSV.replace("ALPHA FUND", "ALPH\xc4 FUND").encode("latin-1")
    )
    with pytest.raises(imp.HandExportError, match="not valid UTF-8"):
        _run(w)


def test_a_dated_looking_row_that_does_not_parse_refuses_instead_of_dropping() -> None:
    bad = TX_CSV.replace('"11/16/2026","Qualified', '"1/16/2026","Qualified')
    with pytest.raises(imp.HandExportError, match="without a valid date"):
        imp.parse_transactions_csv(bad)
    with pytest.raises(imp.HandExportError, match="without a valid date"):
        imp.parse_balances_csv(AVH_CSV + '"1/5/2026","$1.00"\n')


def test_a_capture_day_before_the_cutoff_refuses(tmp_path: Path) -> None:
    with pytest.raises(imp.HandExportError, match="before the cutoff"):
        _run(_world(tmp_path), capture_day=date(2026, 11, 18))


def test_a_positions_page_without_the_total_line_refuses() -> None:
    with pytest.raises(imp.HandExportError, match="Positions Total"):
        imp.parse_positions_page(POSITIONS.replace("Positions Total\n", ""))


@pytest.mark.parametrize("suffix", ["12", "1234", "../1", "a b"])
def test_a_bad_account_suffix_refuses_cleanly(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(imp.HandExportError, match="not 3 letters or digits"):
        _run(_world(tmp_path), accounts=[suffix])
