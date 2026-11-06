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


def _declared(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    run, closes = _world(tmp_path)
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
