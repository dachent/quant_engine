from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from datetime import date

import pytest

import schwab_nav_derive as derive_tool

DAY, PRIOR = date(2026, 9, 17), date(2026, 9, 16)
CROSSWALK = {
    "EXT-FLAT": "account-00000000000000f1",
    "EXT-STALE": "account-00000000000000a2",
    "EXT-MOVED": "account-00000000000000b3",
}


def _holdings(rows: dict[str, tuple[dict[str, str], str]]) -> dict[str, object]:
    accounts = []
    for external, (quantities, cash) in rows.items():
        groups: list[dict[str, object]] = [
            {
                "securityType": 9,
                "holdingsRows": [{"marketValue": {"val": cash}}],
            }
        ]
        if quantities:
            groups.append(
                {
                    "securityType": 1,
                    "holdingsRows": [
                        {"symbol": {"symbol": symbol}, "qty": {"qty": qty}}
                        for symbol, qty in quantities.items()
                    ],
                }
            )
        accounts.append({"accountId": external, "groupedPositions": groups})
    return {"accounts": accounts}


def _history(values: dict[str, float]) -> dict[str, object]:
    return {
        "histories": {
            f"{int(day[5:7])}/{int(day[8:10])}/{day[:4]}": {"AccountValue": value}
            for day, value in values.items()
        }
    }


def _world(
    tmp_path: Path,
    *,
    stale_native: tuple[float, float] = (1000.5, 1000.5),
    moved_native: tuple[float, float] = (500.0, 520.0),
    flat_native: tuple[float, float] = (10.0, 10.0),
    closes: dict[str, dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    run = tmp_path / "run"
    (run / "raw/balances.historical").mkdir(parents=True)
    holdings = _holdings(
        {
            "EXT-FLAT": ({}, "10.00"),
            "EXT-STALE": ({"AAA": "10"}, "100.50"),
            "EXT-MOVED": ({"BBB": "4"}, "20.00"),
        }
    )
    (run / "raw/holdings.v2.json").write_text(json.dumps(holdings), encoding="utf-8")
    natives = {
        "account-00000000000000f1": flat_native,
        "account-00000000000000a2": stale_native,
        "account-00000000000000b3": moved_native,
    }
    for account, (prior, day) in natives.items():
        (run / "raw/balances.historical" / f"{account}.json").write_text(
            json.dumps(_history({"2026-09-16": prior, "2026-09-17": day})),
            encoding="utf-8",
        )
    table = closes or {
        "AAA": {"2026-09-16": "90.00", "2026-09-17": "95.5"},
        "BBB": {"2026-09-16": "120.00", "2026-09-17": "125.00"},
    }
    closes_dir = tmp_path / "closes"
    closes_dir.mkdir()
    for symbol, rows in table.items():
        lines = ["date,close", *(f"{d},{c}" for d, c in rows.items())]
        (closes_dir / f"{symbol}_yahoo_daily.csv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return run, closes_dir


def _derive(run: Path, closes_dir: Path, **extra: object):
    return derive_tool.derive(
        run_dir=run,
        crosswalk=CROSSWALK,
        closes_dir=closes_dir,
        day=DAY,
        prior_day=PRIOR,
        **extra,  # type: ignore[arg-type]
    )


def test_derives_the_record_and_classifies_each_account(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    record, report = _derive(run, closes)
    # stale: 10 x 95.5 + 100.50 = 1055.50 (prior 10 x 90 + 100.50 = 1000.50)
    assert record["account-00000000000000a2"] == {
        "official_0917": "1055.50",
        "official_0916": "1000.50",
        "nav_file_0916": "1000.5",
        "cash": "100.50",
    }
    assert {row["account_id"]: row["status"] for row in report} == {
        "account-00000000000000f1": derive_tool.STATUS_FLAT,
        "account-00000000000000a2": derive_tool.STATUS_PATCH,
        "account-00000000000000b3": derive_tool.STATUS_AGREES,
    }
    assert record["account-00000000000000b3"]["official_0917"] == "520.00"


def test_the_value_rule_is_the_replays_half_even_cents_plus_cash(
    tmp_path: Path,
) -> None:
    run, closes = _world(
        tmp_path,
        stale_native=(1000.51, 1000.51),
        closes={
            "AAA": {"2026-09-16": "90.0005", "2026-09-17": "95.5005"},
            "BBB": {"2026-09-16": "120", "2026-09-17": "125"},
        },
    )
    record, _ = _derive(run, closes)
    # 10 x 90.0005 = 900.005 -> 900.00 (half even) + 100.50
    assert record["account-00000000000000a2"]["official_0916"] == "1000.50"
    # 10 x 95.5005 = 955.005 -> 955.00 (half even) + 100.50
    assert record["account-00000000000000a2"]["official_0917"] == "1055.50"


def test_a_missing_close_for_a_held_symbol_refuses(tmp_path: Path) -> None:
    run, closes = _world(
        tmp_path, closes={"AAA": {"2026-09-16": "90"}, "BBB": {"2026-09-16": "120"}}
    )
    with pytest.raises(
        derive_tool.DeriveError, match="AAA has no official close for 2026-09-17"
    ):
        _derive(run, closes)


def test_a_missing_close_file_refuses(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    (closes / "BBB_yahoo_daily.csv").unlink()
    with pytest.raises(derive_tool.DeriveError, match="no official close file for BBB"):
        _derive(run, closes)


def test_the_prior_day_closure_beyond_the_tolerance_refuses(tmp_path: Path) -> None:
    run, closes = _world(tmp_path, stale_native=(1000.0, 1000.0))
    with pytest.raises(
        derive_tool.DeriveError, match="account-00000000000000a2.*1000.50.*1000.0.*0.05"
    ):
        _derive(run, closes)
    record, _ = _derive(run, closes, tolerance=Decimal("0.50"))
    assert record["account-00000000000000a2"]["official_0916"] == "1000.50"


def test_a_flat_account_whose_native_rows_differ_from_its_cash_refuses(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, flat_native=(10.0, 11.0))
    with pytest.raises(
        derive_tool.DeriveError, match="flat account.*must both equal its cash"
    ):
        _derive(run, closes)


def test_a_non_flat_account_that_moved_and_disagrees_refuses(tmp_path: Path) -> None:
    run, closes = _world(tmp_path, moved_native=(500.0, 600.0))
    with pytest.raises(
        derive_tool.DeriveError, match="account-00000000000000b3.*not a carry"
    ):
        _derive(run, closes)


def test_a_stale_carry_on_a_non_flat_account_is_patched_never_accepted(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path)
    _, report = _derive(run, closes)
    stale = next(
        row for row in report if row["account_id"] == "account-00000000000000a2"
    )
    assert stale["status"] == derive_tool.STATUS_PATCH
    assert stale["native_day"] == "1000.5"
    assert stale["official_day"] == "1055.50"


def test_a_native_history_lacking_a_day_refuses(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    (run / "raw/balances.historical/account-00000000000000a2.json").write_text(
        json.dumps(_history({"2026-09-16": 1000.5})), encoding="utf-8"
    )
    with pytest.raises(derive_tool.DeriveError, match="lacks 2026-09-16 or 2026-09-17"):
        _derive(run, closes)


def test_a_crosswalk_account_missing_from_the_holdings_refuses(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    with pytest.raises(
        derive_tool.DeriveError, match="lacks accounts: account-00000000000000e4"
    ):
        derive_tool.derive(
            run_dir=run,
            crosswalk={**CROSSWALK, "EXT-EXTRA": "account-00000000000000e4"},
            closes_dir=closes,
            day=DAY,
            prior_day=PRIOR,
        )


def test_the_prior_day_must_precede_the_day(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    with pytest.raises(derive_tool.DeriveError, match="must precede"):
        derive_tool.derive(
            run_dir=run,
            crosswalk=CROSSWALK,
            closes_dir=closes,
            day=PRIOR,
            prior_day=DAY,
        )


def test_the_command_writes_create_only_and_reports_the_closure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, closes = _world(tmp_path)
    crosswalk = tmp_path / "crosswalk.json"
    crosswalk.write_text(
        json.dumps(
            {
                "crosswalk": [
                    {"external_account_id": ext, "internal_account_id": internal}
                    for ext, internal in CROSSWALK.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    out, report = tmp_path / "derivation.json", tmp_path / "report.json"
    argv = [
        "derive",
        "--run-dir", str(run),
        "--crosswalk", str(crosswalk),
        "--closes-dir", str(closes),
        "--day", "2026-09-17",
        "--prior-day", "2026-09-16",
        "--out", str(out),
        "--report", str(report),
    ]  # fmt: skip
    assert derive_tool.main(argv) == 0
    assert (
        json.loads(out.read_text(encoding="utf-8"))["account-00000000000000a2"]["cash"]
        == "100.50"
    )
    assert "account-00000000000000a2: STALE_CARRY_PATCHED" in capsys.readouterr().out
    assert derive_tool.main(argv) == 1  # create-only
    assert "FAIL" in capsys.readouterr().err


# -- the declared evidence is replayed by the provider ledger ---------------------------------


def _ledger() -> object:
    import provider_ledger

    return provider_ledger


def _declared(
    tmp_path: Path,
    *,
    stale_native: tuple[float, float] = (1000.5, 1000.5),
    rows: list[dict[str, str]] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    run, closes = _world(tmp_path, stale_native=stale_native)
    if rows:
        _rows(run, "account-00000000000000a2", rows)
    record, _ = _derive(run, closes)
    derivation = tmp_path / "derivation.json"
    derivation.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "roots": {"ingest-nav": "C:/nav"},
                "sources": [
                    {"role": "WCH_NAV_CSV", "account_id": "account-00000000000000a2"},
                    {"role": "WCH_TRANSACTION_JSON", "account_id": "x"},
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence = derive_tool.declare(
        run_dir=run,
        crosswalk=CROSSWALK,
        closes_dir=closes,
        day=DAY,
        prior_day=PRIOR,
        tolerance=derive_tool.DEFAULT_TOLERANCE,
        derivation=derivation,
        spec=spec,
    )
    catalog = {
        "journal_counterparty_crosswalk": {
            "entries": [
                {"account_id": internal, "native_suffix": external[-3:]}
                for external, internal in CROSSWALK.items()
            ]
        }
    }
    return evidence, catalog


def test_the_declared_evidence_is_replayed_exactly_by_the_ledger(
    tmp_path: Path,
) -> None:
    ledger = _ledger()
    evidence, catalog = _declared(tmp_path)
    assert [s["role"] for s in evidence["sources"]] == ["WCH_NAV_CSV"]  # type: ignore[index, union-attr]
    patch = ledger._schwab_source_derived_patch(evidence, DAY, catalog)  # type: ignore[attr-defined]
    assert set(patch.entries) == {"account-00000000000000a2"}
    values = {PRIOR: Decimal("1000.5"), DAY: Decimal("1055.50")}
    ledger._check_schwab_source_cutoff_row(  # type: ignore[attr-defined]
        patch, "account-00000000000000a2", values, DAY
    )
    assert derive_tool.REPLAY_ROUNDING == ledger._SCHWAB_REPLAY_ROUNDING  # type: ignore[attr-defined]


def test_a_derivation_file_that_differs_from_the_run_is_refused(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    record, _ = _derive(run, closes)
    record["account-00000000000000a2"]["official_0917"] = "1.00"
    derivation = tmp_path / "derivation.json"
    derivation.write_text(json.dumps(record), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"roots": {}, "sources": []}), encoding="utf-8")
    with pytest.raises(derive_tool.DeriveError, match="differs from the one derived"):
        derive_tool.declare(
            run_dir=run,
            crosswalk=CROSSWALK,
            closes_dir=closes,
            day=DAY,
            prior_day=PRIOR,
            tolerance=derive_tool.DEFAULT_TOLERANCE,
            derivation=derivation,
            spec=spec,
        )


def test_native_equal_to_official_on_a_carry_is_not_patched(tmp_path: Path) -> None:
    # the carried value happens to be the official one: nothing to patch
    run, closes = _world(tmp_path, stale_native=(1055.5, 1055.5))
    record, report = _derive(run, closes, tolerance=Decimal("100"))
    stale = next(row for row in report if row["account_id"].endswith("a2"))
    assert stale["status"] == derive_tool.STATUS_AGREES


def test_a_slash_symbol_is_read_from_the_dot_named_close_file(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    holdings = run / "raw/holdings.v2.json"
    holdings.write_text(
        holdings.read_text(encoding="utf-8").replace('"AAA"', '"BRK/B"'),
        encoding="utf-8",
    )
    (closes / "AAA_yahoo_daily.csv").rename(closes / "BRK.B_yahoo_daily.csv")
    assert derive_tool.close_file(closes, "BRK/B").name == "BRK.B_yahoo_daily.csv"
    record, _ = _derive(run, closes)
    assert record["account-00000000000000a2"]["official_0917"] == "1055.50"
    (closes / "BRK.B_yahoo_daily.csv").rename(closes / "BRK_B_yahoo_daily.csv")
    with pytest.raises(
        derive_tool.DeriveError, match="no official close file for BRK/B"
    ):
        _derive(run, closes)


# -- an account that traded or moved cash on D can fail the prior-day cross-check honestly -----------

STALE = "account-00000000000000a2"
FLAT = "account-00000000000000f1"
MOVED = "account-00000000000000b3"


def _transactions(run: Path, account: str, *days: str) -> None:
    folder = run / "transactions/normalized/wch/transactions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{account}.json").write_text(
        json.dumps([{"transactionDate": day, "action": "Buy"} for day in days]),
        encoding="utf-8",
    )


def test_a_failed_prior_closure_on_an_active_account_names_the_rows_and_the_waiver(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, stale_native=(1000.0, 1000.0))
    _transactions(run, STALE, "09/17/2026", "09/17/2026", "09/16/2026")
    with pytest.raises(
        derive_tool.DeriveError,
        match=r"2 transaction row\(s\) are dated 2026-09-17.*--waive-prior-closure",
    ):
        _derive(run, closes)


def test_a_waiver_skips_the_prior_closure_for_an_active_account_and_is_recorded(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, stale_native=(1000.0, 1000.0))
    _transactions(run, STALE, "09/17/2026")
    record, report = _derive(run, closes, waivers={STALE: "bought AAA on D"})
    # the derived D value is the end-of-D holdings at the D closes, whatever traded on D
    assert record[STALE]["official_0917"] == "1055.50"
    assert record[STALE]["prior_closure"] == derive_tool.PRIOR_CLOSURE_WAIVED
    assert record[STALE]["waiver_reason"] == "bought AAA on D"
    assert record[STALE]["transaction_rows_on_day"] == "1"
    row = next(r for r in report if r["account_id"] == STALE)
    assert row["status"] == derive_tool.STATUS_PATCH
    assert row["prior_closure"] == derive_tool.PRIOR_CLOSURE_WAIVED


def test_a_flat_account_with_a_cash_flow_on_the_day_needs_the_waiver(
    tmp_path: Path,
) -> None:
    # native NAV 5.00 on D-1 and (stale) on D; a deposit on D left 10.00 of cash
    run, closes = _world(tmp_path, flat_native=(5.0, 5.0))
    _transactions(run, FLAT, "09/17/2026")
    with pytest.raises(derive_tool.DeriveError, match="1 transaction row"):
        _derive(run, closes)
    record, report = _derive(run, closes, waivers={FLAT: "deposit on D"})
    assert record[FLAT]["official_0917"] == "10.00"
    flat = next(r for r in report if r["account_id"] == FLAT)
    assert flat["status"] == derive_tool.STATUS_PATCH


def test_a_waiver_for_an_account_without_transactions_on_the_day_is_refused(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, stale_native=(1000.0, 1000.0))
    _transactions(run, STALE, "09/16/2026")
    with pytest.raises(
        derive_tool.DeriveError, match="a waiver needs transactions dated"
    ):
        _derive(run, closes, waivers={STALE: "no"})


def test_a_waiver_naming_an_unknown_account_is_refused(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    with pytest.raises(derive_tool.DeriveError, match="outside the crosswalk"):
        _derive(run, closes, waivers={"account-0000000000000099": "x"})


def test_waivers_are_account_equals_reason_and_never_repeated() -> None:
    assert derive_tool._waivers(["a=because", "b=c d"]) == {"a": "because", "b": "c d"}
    for bad in (["a"], ["=x"], ["a="], ["a=x", "a=y"]):
        with pytest.raises(derive_tool.DeriveError):
            derive_tool._waivers(bad)


def _rows(run: Path, account: str, rows: list[dict[str, str]]) -> None:
    folder = run / "transactions/normalized/wch/transactions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{account}.json").write_text(json.dumps(rows), encoding="utf-8")


SELL_AFTER_D = {
    "transactionDate": "09/18/2026",
    "effectiveDate": "09/18/2026",
    "action": "Sell",
    "symbol": "AAA",
    "shareQuantity": "1",
    "amount": "$96.00",
}


def test_a_capture_taken_after_d_is_rolled_back_to_d_before_pricing(
    tmp_path: Path,
) -> None:
    # captured: 10 AAA and 100.50 cash after a Sell of 1 AAA for 96.00 posted the day after D;
    # at the close of D the account held 11 AAA and 4.50 cash
    run, closes = _world(tmp_path, stale_native=(994.5, 994.5))
    _rows(run, STALE, [SELL_AFTER_D])
    record, report = _derive(run, closes)
    assert record[STALE]["official_0917"] == "1055.00"  # 11 x 95.50 + 4.50
    assert record[STALE]["cash"] == "4.50"
    assert record[STALE]["rolled_back_rows"] == "Sell x1"
    assert next(r for r in report if r["account_id"] == STALE)["status"] == (
        derive_tool.STATUS_PATCH
    )
    assert "rolled_back_rows" not in record[MOVED]


def test_a_roll_back_that_disagrees_with_the_native_history_stops_with_the_numbers(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path)  # native 1000.5 on both days
    _rows(run, STALE, [SELL_AFTER_D])
    with pytest.raises(derive_tool.DeriveError, match=r"differs from the captured NAV"):
        _derive(run, closes)


@pytest.mark.parametrize("action", ["Stock Split", "Spin-off", "Merger", "Mystery"])
def test_an_action_outside_the_table_after_d_is_refused_by_name(
    tmp_path: Path, action: str
) -> None:
    run, closes = _world(tmp_path)
    _rows(
        run,
        STALE,
        [{**SELL_AFTER_D, "action": action}],
    )
    with pytest.raises(
        derive_tool.DeriveError, match=f"a {action} row dated 2026-09-18"
    ):
        _derive(run, closes)


STRADDLER = {
    "transactionDate": "09/18/2026 as of 09/17/2026",
    "effectiveDate": "09/17/2026",
    "action": "Cash Dividend",
    "symbol": "BBB",
    "amount": "$50.00",
}


def test_a_straddler_the_native_balance_places_in_the_day_is_taken_in_the_day(
    tmp_path: Path,
) -> None:
    # captured cash 20.00 already holds a 50.00 dividend posted after D "as of" D. Counted in D the
    # close of D is 4 x 125 + 20.00 = 520.00, the native balance; counted after D it is 470.00.
    run, closes = _world(tmp_path)
    _rows(run, MOVED, [STRADDLER])
    record, _ = _derive(run, closes)
    assert record[MOVED]["straddler_reading"] == "IN_D"
    assert record[MOVED]["straddler_rows"] == "Cash Dividend x1"
    assert record[MOVED]["straddler_official_in_d"] == "520.00"
    assert record[MOVED]["straddler_official_out_d"] == "470.00"
    assert record[MOVED]["official_0917"] == "520.00"
    assert "rolled_back_rows" not in record[MOVED]


def test_a_straddler_the_native_balance_places_after_the_day_is_rolled_back(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, moved_native=(450.0, 470.0))
    _rows(run, MOVED, [STRADDLER])
    record, _ = _derive(run, closes)
    assert record[MOVED]["straddler_reading"] == "OUT_D"
    assert record[MOVED]["official_0917"] == "470.00"
    assert record[MOVED]["cash"] == "-30.00"
    assert record[MOVED]["rolled_back_rows"] == "Cash Dividend x1"


def test_a_straddler_that_both_readings_match_is_ambiguous_and_stops(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path)
    _rows(run, MOVED, [{**STRADDLER, "amount": "$0.01"}])
    with pytest.raises(derive_tool.DeriveError, match="matches both readings"):
        _derive(run, closes)


def test_a_straddler_that_neither_reading_matches_stops_with_both_values(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, moved_native=(500.0, 999.0))
    _rows(run, MOVED, [STRADDLER])
    with pytest.raises(
        derive_tool.DeriveError, match=r"matches neither.*520\.00.*470\.00"
    ):
        _derive(run, closes)


def test_a_straddler_on_a_carried_native_row_cannot_be_decided(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, stale_native=(1055.5, 1055.5))
    _rows(run, STALE, [STRADDLER])
    with pytest.raises(derive_tool.DeriveError, match="repeats the day before"):
        _derive(run, closes)


def test_the_ledger_replays_the_reading_the_derivation_stated(tmp_path: Path) -> None:
    ledger = _ledger()
    evidence, catalog = _declared(tmp_path, rows=None)
    run = Path(
        evidence["derived_patch"]["replay"]["positions_capture"]["path"]
    ).parents[1]  # type: ignore[index]
    # a straddler on the unpatched account is placed by the record and replayed by the ledger
    folder = run / "transactions/normalized/wch/transactions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{MOVED}.json").write_text(json.dumps([STRADDLER]), encoding="utf-8")
    closes = tmp_path / "closes"
    record, _ = derive_tool.derive(
        run_dir=run, crosswalk=CROSSWALK, closes_dir=closes, day=DAY, prior_day=PRIOR
    )
    derivation = tmp_path / "derivation2.json"
    derivation.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    spec = tmp_path / "spec2.json"
    spec.write_text(json.dumps({"roots": {}, "sources": []}), encoding="utf-8")
    declared = derive_tool.declare(
        run_dir=run,
        crosswalk=CROSSWALK,
        closes_dir=closes,
        day=DAY,
        prior_day=PRIOR,
        tolerance=derive_tool.DEFAULT_TOLERANCE,
        derivation=derivation,
        spec=spec,
    )
    ledger._schwab_source_derived_patch(declared, DAY, catalog)  # type: ignore[attr-defined]
    record[MOVED].pop("straddler_reading")
    derivation.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    # the record is pinned by hash: an edited one no longer matches the declaration
    with pytest.raises(Exception, match="(?i)pin|sha|differ"):
        ledger._schwab_source_derived_patch(declared, DAY, catalog)  # type: ignore[attr-defined]


def test_a_row_after_d_with_no_quantity_or_amount_is_recorded_not_rolled_back(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path)
    _rows(
        run,
        STALE,
        [
            {"transactionDate": "09/18/2026", "action": "Memo", "shareQuantity": ""},
            {"transactionDate": "09/18/2026", "action": "Memo"},
        ],
    )
    record, _ = _derive(run, closes)
    assert record[STALE]["after_day_informational_rows"] == "Memo x2"
    assert "rolled_back_rows" not in record[STALE]
    assert "after_day_informational_rows" not in record[MOVED]


def test_rows_on_or_before_d_do_not_change_the_derivation(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    base, _ = _derive(run, closes)
    _rows(
        run,
        STALE,
        [{"transactionDate": "09/17/2026", "action": "Buy", "shareQuantity": "1"}],
    )
    with_rows, _ = _derive(run, closes, waivers={STALE: "bought AAA on D"})
    assert "rolled_back_rows" not in with_rows[STALE]
    assert base[MOVED] == with_rows[MOVED]


def test_an_unreadable_posting_date_is_refused(tmp_path: Path) -> None:
    run, closes = _world(tmp_path)
    _rows(run, STALE, [{"transactionDate": "yesterday", "action": "Buy"}])
    with pytest.raises(derive_tool.DeriveError, match="unreadable date"):
        _derive(run, closes)


def test_a_symbol_sold_in_full_after_d_is_priced_at_d_from_its_own_close_file(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path, stale_native=(1043.5, 1043.5))
    (closes / "CCC_yahoo_daily.csv").write_text(
        "date,close\n2026-09-16,10.00\n2026-09-17,11.00\n", encoding="utf-8"
    )
    _rows(
        run,
        STALE,
        [
            {
                **SELL_AFTER_D,
                "symbol": "CCC",
                "shareQuantity": "2",
                "amount": "$22.00",
            },
            {
                **SELL_AFTER_D,
                "symbol": "AAA",
                "shareQuantity": "0.5",
                "amount": "$0.00",
            },
        ],
    )
    # D holdings: 10.5 AAA, 2 CCC, cash 78.50: 1043.50 at the D-1 closes, 1103.25 at D
    record, _ = _derive(run, closes)
    assert record[STALE]["cash"] == "78.50"
    assert record[STALE]["rolled_back_rows"] == "Sell x2"


def test_a_symbol_without_a_close_file_at_d_stops_the_derivation(
    tmp_path: Path,
) -> None:
    run, closes = _world(tmp_path)
    _rows(run, STALE, [{**SELL_AFTER_D, "symbol": "CCC", "shareQuantity": "2"}])
    with pytest.raises(derive_tool.DeriveError, match="no official close file for CCC"):
        _derive(run, closes)


def test_a_rolled_back_derivation_is_replayed_exactly_by_the_ledger(
    tmp_path: Path,
) -> None:
    ledger = _ledger()
    evidence, catalog = _declared(
        tmp_path, stale_native=(994.5, 994.5), rows=[SELL_AFTER_D]
    )
    replay = evidence["derived_patch"]["replay"]  # type: ignore[index]
    assert [p["account_id"] for p in replay["transactions"]] == [
        "account-00000000000000a2"
    ]
    patch = ledger._schwab_source_derived_patch(evidence, DAY, catalog)  # type: ignore[attr-defined]
    values = {PRIOR: Decimal("994.5"), DAY: Decimal("1055.00")}
    ledger._check_schwab_source_cutoff_row(  # type: ignore[attr-defined]
        patch, "account-00000000000000a2", values, DAY
    )


def test_the_ledger_refuses_a_replay_whose_transactions_are_altered_or_dropped(
    tmp_path: Path,
) -> None:
    ledger = _ledger()
    evidence, catalog = _declared(
        tmp_path, stale_native=(994.5, 994.5), rows=[SELL_AFTER_D]
    )
    pinned = Path(evidence["derived_patch"]["replay"]["transactions"][0]["path"])  # type: ignore[index]
    original = pinned.read_text(encoding="utf-8")
    pinned.write_text(original.replace('"$96.00"', '"$97.00"'), encoding="utf-8")
    with pytest.raises(Exception, match="(?i)pin|sha|differ"):
        ledger._schwab_source_derived_patch(evidence, DAY, catalog)  # type: ignore[attr-defined]
    pinned.write_text(original, encoding="utf-8")
    dropped = json.loads(json.dumps(evidence))
    del dropped["derived_patch"]["replay"]["transactions"]
    with pytest.raises(Exception, match="differ"):
        ledger._schwab_source_derived_patch(dropped, DAY, catalog)  # type: ignore[attr-defined]


def test_the_transactions_the_guard_read_are_pinned_and_checked_by_the_ledger(
    tmp_path: Path,
) -> None:
    ledger = _ledger()
    run, closes = _world(tmp_path)
    _rows(
        run,
        "account-00000000000000a2",
        [{"transactionDate": "09/16/2026", "action": "Buy", "shareQuantity": "1"}],
    )
    record, _ = _derive(run, closes)
    derivation = tmp_path / "derivation.json"
    derivation.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"roots": {}, "sources": []}), encoding="utf-8")
    evidence = derive_tool.declare(
        run_dir=run,
        crosswalk=CROSSWALK,
        closes_dir=closes,
        day=DAY,
        prior_day=PRIOR,
        tolerance=derive_tool.DEFAULT_TOLERANCE,
        derivation=derivation,
        spec=spec,
    )
    catalog = {
        "journal_counterparty_crosswalk": {
            "entries": [
                {"account_id": internal, "native_suffix": external[-3:]}
                for external, internal in CROSSWALK.items()
            ]
        }
    }
    pinned = evidence["derived_patch"]["replay"]["transactions"]  # type: ignore[index]
    assert [p["account_id"] for p in pinned] == ["account-00000000000000a2"]
    ledger._schwab_source_derived_patch(evidence, DAY, catalog)  # type: ignore[attr-defined]
    path = Path(pinned[0]["path"])
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("09/16/2026", "09/18/2026"), encoding="utf-8")
    with pytest.raises(Exception, match="(?i)pin|sha|differ"):
        ledger._schwab_source_derived_patch(evidence, DAY, catalog)  # type: ignore[attr-defined]
    path.unlink()
    with pytest.raises(Exception, match="regular file"):
        ledger._schwab_source_derived_patch(evidence, DAY, catalog)  # type: ignore[attr-defined]
