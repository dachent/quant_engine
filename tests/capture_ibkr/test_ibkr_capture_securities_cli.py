from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import quant_engine.ibkr.generation as ibkr_generation
from ibkr_capture_fixtures import (
    ACCOUNT_ONE,
    FAKE_A,
    FakeHttp,
    NOW,
    activity_xml,
    credential_config,
    fake_source_for,
    make_plan,
    send_ok,
    security_xml,
    weekday_range,
    write_evidence,
)
from datetime import date
from quant_engine.ibkr.capture import (
    SymbolTable,
    ledger_security_id,
    scan_securities,
    security_key,
)
from quant_engine.ibkr.capture.cli import main, run_live
from quant_engine.ibkr.capture.securities import SecurityScanError


def test_key_matches_the_ledger_security_id_convention() -> None:
    for conid in ("1", "900001", "123456789"):
        assert ledger_security_id(conid) == ibkr_generation._security_id(conid)  # type: ignore[attr-defined]
        assert security_key(conid) == hashlib.sha256(conid.encode()).hexdigest()[:24]
        assert len(security_key(conid)) == 24


@pytest.mark.parametrize("conid", ["", "abc", "12 3", "-5", "1" * 19])
def test_invalid_conids_are_refused(conid: str) -> None:
    with pytest.raises(SecurityScanError):
        security_key(conid)


def _rows(xml: bytes, tmp_path: Path) -> list[dict[str, object]]:
    from quant_engine.ibkr.capture.securities import aggregate_rows

    path = tmp_path / "x.xml"
    path.write_bytes(xml)
    return aggregate_rows(scan_securities(path))


def test_resolver_statuses_and_exact_unresolved_report(tmp_path: Path) -> None:
    rows = _rows(
        activity_xml(
            (ACCOUNT_ONE,), weekday_range(date(2026, 10, 5), date(2026, 10, 6))
        ),
        tmp_path,
    )
    table = SymbolTable.from_rows(rows)
    resolved = table.lookup(ledger_security_id("900001"))
    assert (resolved.status, resolved.symbol, resolved.asset_category) == (
        "RESOLVED",
        "SYNA",
        "STK",
    )
    # 900003 appears only on Trade rows that carry no symbol.
    assert table.lookup(security_key("900003")).status == "MISSING_SYMBOL"
    unseen = table.lookup(security_key("424242"))
    assert unseen.status == "NOT_OBSERVED"
    report = table.report(
        [ledger_security_id("900001"), security_key("900003"), security_key("424242")]
    )
    assert report["required"] == 3 and report["resolved"] == 1
    assert report["unresolved"] == [
        {"key": security_key("424242"), "status": "NOT_OBSERVED"},
        {"key": security_key("900003"), "status": "MISSING_SYMBOL"},
    ] or {item["key"] for item in report["unresolved"]} == {
        security_key("424242"),
        security_key("900003"),
    }


def test_conflicting_symbols_are_ambiguous_not_guessed(tmp_path: Path) -> None:
    first = _rows(
        security_xml((ACCOUNT_ONE,), date(2026, 10, 5), date(2026, 10, 9)), tmp_path
    )
    renamed = security_xml(
        (ACCOUNT_ONE,), date(2026, 10, 12), date(2026, 10, 16)
    ).replace(b'symbol="SYNX"', b'symbol="SYNY"')
    other = tmp_path / "other"
    other.mkdir()
    second = _rows(renamed, other)
    table = SymbolTable.from_rows([*first, *second])
    entry = table.lookup(security_key("910001"))
    assert entry.status == "AMBIGUOUS" and entry.symbol is None
    assert entry.symbols == ("SYNX", "SYNY")
    assert table.lookup(security_key("910002")).status == "MISSING_SYMBOL"
    document = table.to_document()
    assert document["schema"] == "ibkr-symbol-table-v1"


def test_a_missing_category_is_reported_distinctly() -> None:
    table = SymbolTable.from_rows(
        [
            {
                "key": security_key("5"),
                "symbols": [{"symbol": "SYNZ"}],
                "asset_categories": [],
                "rows": 1,
            }
        ]
    )
    assert table.lookup(security_key("5")).status == "MISSING_ASSET_CATEGORY"


def test_scan_refuses_declarations_and_bad_conids(tmp_path: Path) -> None:
    path = tmp_path / "bad.xml"
    path.write_bytes(b'<!DOCTYPE x [<!ENTITY a "b">]><FlexQueryResponse/>')
    with pytest.raises(ValueError):
        scan_securities(path)
    path2 = tmp_path / "bad2.xml"
    path2.write_bytes(b'<A><Trade conid="x1" symbol="S"/></A>')
    with pytest.raises(SecurityScanError):
        scan_securities(path2)


def test_cli_plan_hash_and_governance_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = make_plan()
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(plan.canonical_bytes())
    assert main(["plan-hash", str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["plan_sha256"] == plan.sha256
    # Live capture cannot even be parsed without governance evidence.
    with pytest.raises(SystemExit):
        main(["live", "--plan", str(plan_path)])
    capsys.readouterr()
    missing = main(
        [
            "live",
            "--plan",
            str(plan_path),
            "--governance",
            str(tmp_path / "none.json"),
            "--config",
            str(tmp_path / "c.toml"),
            "--receipt-root",
            str(tmp_path),
            "--staging-root",
            str(tmp_path),
            "--user-agent",
            "x",
        ]
    )
    assert missing == 2
    assert "GovernanceError" in capsys.readouterr().out


def test_live_path_runs_with_injected_transport_and_orders_governance_first(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(plan.canonical_bytes())
    for name in ("receipts", "stage", "ev"):
        (tmp_path / name).mkdir()
    evidence = write_evidence(tmp_path / "ev", plan)
    config = tmp_path / "creds.toml"
    config.write_text(
        "[credentials.ibkr]\n"
        'flex_token = [{ source = "windows-credential-manager", name = "target-flex-a" }]\n'
        'query_activity_history = [{ source = "windows-credential-manager", name = "target-query-0" }]\n',
        encoding="utf-8",
    )
    source = fake_source_for("query_activity_history")
    http = FakeHttp(
        [
            send_ok(),
            activity_xml(
                (ACCOUNT_ONE,), weekday_range(date(2026, 10, 5), date(2026, 10, 9))
            ),
        ]
    )
    waits: list[float] = []
    summaries = run_live(
        plan_path=plan_path,
        governance_path=evidence,
        config_path=config,
        receipt_root=tmp_path / "receipts",
        staging_root=tmp_path / "stage",
        user_agent="quant-engine-synthetic-test",
        http_get=http,
        wait=waits.append,
        now=lambda: NOW,
        sources={"windows-credential-manager": source},
    )
    assert [s.status for s in summaries] == ["STAGED"]
    assert http.calls[0][1]["t"] == FAKE_A

    # A consumed (or invalid) approval refuses before any credential is read.
    fresh = fake_source_for("query_activity_history")
    http2 = FakeHttp([])
    with pytest.raises(Exception, match="already consumed"):
        run_live(
            plan_path=plan_path,
            governance_path=evidence,
            config_path=config,
            receipt_root=tmp_path / "receipts",
            staging_root=tmp_path / "stage",
            user_agent="x",
            http_get=http2,
            now=lambda: NOW,
            sources={"windows-credential-manager": fresh},
        )
    assert fresh.reads == [] and http2.calls == []


def test_cli_offline_commands_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ibkr_capture_fixtures import Harness

    harness = Harness(
        tmp_path,
        [send_ok(), security_xml((ACCOUNT_ONE,), date(2026, 10, 5), date(2026, 10, 9))],
        plan=make_plan(query="security-reference"),
    )
    harness.run()
    (run_dir,) = harness.run_dirs()
    normalized = tmp_path / "n.json"
    assert main(["normalize", str(run_dir), str(normalized)]) == 0
    capsys.readouterr()
    assert main(["verify", str(run_dir), str(normalized)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"
    required = tmp_path / "required.txt"
    required.write_text(
        f"{ledger_security_id('910001')}\n{ledger_security_id('910002')}\n",
        encoding="utf-8",
    )
    table = tmp_path / "table.json"
    assert (
        main(
            [
                "symbols",
                "--normalized",
                str(normalized),
                "--required",
                str(required),
                "--output",
                str(table),
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["report"]["resolved"] == 1
    assert summary["report"]["unresolved"] == [
        {"key": security_key("910002"), "status": "MISSING_SYMBOL"}
    ]
    # Create-only: a second table at the same path is refused.
    assert (
        main(["symbols", "--normalized", str(normalized), "--output", str(table)]) == 2
    )
    assert main(["verify", str(run_dir), str(tmp_path / "none.json")]) == 2
    assert "normalized-missing" in capsys.readouterr().out
    assert credential_config  # fixture import used by sibling modules
