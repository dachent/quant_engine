from __future__ import annotations

from collections import Counter
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.schwab.refresh_normalize import RefreshNormalizeError, normalize


NAV_ROWS = 5
POSITION_ROWS = 3
PAGES_PER_ACCOUNT = 2
RECORDS_PER_PAGE = 2


def _record(account_index: int, page_index: int, record_index: int) -> dict[str, Any]:
    return {
        "transactionDate": "08/15/2026",
        "action": "Buy",
        "symbol": f"SYN{account_index}",
        "description": (
            f"SYNTHETIC TRADE A{account_index} P{page_index} R{record_index}"
        ),
        "shareQuantity": "1",
        "executionPrice": "10.00",
        "feesAndCommission": "0.00",
        "amount": "-10.00",
        "sourceCode": "SYNTH",
        "effectiveDate": "08/15/2026",
        "transactionDetail": {"settleDate": "08/17/2026"},
        "showTransactionDetailLink": True,
        "showSourceCodeLink": False,
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _build_run(
    run_root: Path,
    accounts: list[str],
    *,
    drop_key: str | None = None,
    terminal_bookmark: object = None,
) -> None:
    for account_index, account_id in enumerate(accounts):
        for page_index in range(PAGES_PER_ACCOUNT):
            records = [
                _record(account_index, page_index, record_index)
                for record_index in range(RECORDS_PER_PAGE)
            ]
            if drop_key is not None and account_index == 0 and page_index == 0:
                for record in records:
                    record.pop(drop_key, None)
            bookmark = (
                terminal_bookmark
                if page_index == PAGES_PER_ACCOUNT - 1
                else {"next": f"page-{page_index + 1}"}
            )
            envelope = {
                "fromDate": "2026-08-01T00:00:00.000Z",
                "toDate": "2026-09-02T00:00:00.000Z",
                "totalTransactionsAmount": "-20.00",
                "totalFeesAndCommAmount": "0.00",
                "brokerageTransactions": records,
                "bookmark": bookmark,
            }
            page_dir = run_root / "pages" / account_id
            _write_json(page_dir / f"{page_index:04d}.json", envelope)
            _write_json(
                page_dir / f"{page_index:04d}.request.json",
                {"url": "https://example.invalid/synthetic"},
            )
    _write_json(run_root / "snapshots" / "accounts_view.json", {"synthetic": True})
    _write_json(run_root / "snapshots" / "customer_accounts.json", {"synthetic": True})
    _write_manifest(run_root, accounts)


def _write_manifest(run_root: Path, accounts: list[str]) -> None:
    artifacts: list[dict[str, object]] = []
    for section in ("pages", "snapshots"):
        for path in sorted((run_root / section).rglob("*")):
            if path.is_file():
                payload = path.read_bytes()
                artifacts.append(
                    {
                        "path": path.relative_to(run_root).as_posix(),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                    }
                )
    manifest = {
        "accounts": {
            account_id: {
                "page_count": PAGES_PER_ACCOUNT,
                "status": "TERMINAL",
                "terminal": True,
                "transaction_count": PAGES_PER_ACCOUNT * RECORDS_PER_PAGE,
            }
            for account_id in accounts
        },
        "artifacts": artifacts,
        "context_mode": "SYNTHETIC",
        "finished_at": "2026-09-02T21:19:01.711+00:00",
        "run_id": "synthetic-run-20260902",
        "started_at": "2026-09-02T21:10:00.000+00:00",
    }
    _write_json(run_root / "manifest.json", manifest)


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _prior_source(
    path: Path,
    *,
    root: Path,
    root_id: str,
    artifact_id: str,
    role: str,
    account_id: str | None,
    schema_name: str,
    record_count: int | None = None,
) -> dict[str, object]:
    payload = path.read_bytes()
    source: dict[str, object] = {
        "role": role,
        "account_id": account_id,
        "artifact_id": artifact_id,
        "as_of_date": "2026-08-14",
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "relative_path": path.relative_to(root).as_posix(),
        "root_id": root_id,
        "schema_name": schema_name,
        "schema_version": "1",
    }
    if record_count is not None:
        source["expected_record_count"] = record_count
    return source


def _build_prior(prior_root: Path, accounts: list[str]) -> Path:
    har = prior_root / "schwab_har"
    legacy = prior_root / "Strategies_dataSchwab"
    sources: list[dict[str, object]] = []
    for account_id in accounts:
        wch_transaction = (
            har / "playwright_json" / f"transactions_{account_id}_wch.json"
        )
        _write_json(wch_transaction, [_record(0, 0, 0)])
        sources.append(
            _prior_source(
                wch_transaction,
                root=har,
                root_id="har",
                artifact_id=f"playwright_json/transactions_{account_id}_wch.json",
                role="WCH_TRANSACTION_JSON",
                account_id=account_id,
                schema_name="schwab-wch-transaction-history",
            )
        )
    for account_id in accounts:
        wch_nav = har / "playwright_csv" / f"historical_balances_{account_id}.csv"
        _write_csv(
            wch_nav,
            ["account_id", "account_name", "date", "account_value"],
            [[account_id, "SYNTHETIC", "08/14/2026", "1000.00"]],
        )
        sources.append(
            _prior_source(
                wch_nav,
                root=har,
                root_id="har",
                artifact_id=f"playwright_csv/historical_balances_{account_id}.csv",
                role="WCH_NAV_CSV",
                account_id=account_id,
                schema_name="schwab-wch-historical-balance",
            )
        )
    manual_transactions = har / "manual_verification" / "manual_transactions.json"
    _write_json(
        manual_transactions,
        {
            "FromDate": "08/01/2026",
            "ToDate": "08/14/2026",
            "BrokerageTransactions": [
                {
                    "Date": "08/14/2026",
                    "Action": "Buy",
                    "Symbol": "SYN0",
                    "Description": "SYNTHETIC TRADE",
                    "Quantity": "1",
                    "Price": "10.00",
                    "Fees & Comm": "0.00",
                    "Amount": "-10.00",
                }
            ],
        },
    )
    sources.append(
        _prior_source(
            manual_transactions,
            root=har,
            root_id="har",
            artifact_id="manual_verification/manual_transactions.json",
            role="MANUAL_TRANSACTION_JSON_ALL",
            account_id=None,
            schema_name="schwab-manual-transaction-export",
            record_count=1,
        )
    )
    manual_nav = har / "manual_verification" / "all_accounts_balances.CSV"
    _write_csv(
        manual_nav,
        ["Date", "Amount"],
        [["08/14/2026", f"{1000.00 + index:.2f}"] for index in range(NAV_ROWS)],
    )
    sources.append(
        _prior_source(
            manual_nav,
            root=har,
            root_id="har",
            artifact_id="manual_verification/all_accounts_balances.CSV",
            role="MANUAL_NAV_CSV_ALL",
            account_id=None,
            schema_name="schwab-manual-nav-export",
        )
    )
    manual_positions = har / "manual_verification" / "all_accounts_positions.csv"
    _write_csv(
        manual_positions,
        ["Symbol", "Qty (Quantity)"],
        [[f"POS{index}", str(index + 1)] for index in range(POSITION_ROWS)],
    )
    sources.append(
        _prior_source(
            manual_positions,
            root=har,
            root_id="har",
            artifact_id="manual_verification/all_accounts_positions.csv",
            role="MANUAL_POSITION_CSV",
            account_id=None,
            schema_name="schwab-manual-position-export",
        )
    )
    workbook = legacy / "Schwab Brokerage Data.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"SYNTHETIC-WORKBOOK")
    sources.append(
        _prior_source(
            workbook,
            root=legacy,
            root_id="legacy",
            artifact_id="Schwab Brokerage Data.xlsx",
            role="WORKBOOK_REFERENCE",
            account_id=None,
            schema_name="schwab-workbook-reference",
        )
    )
    snapshot = prior_root / "normalized" / "schwab-web-current-snapshot.json"
    _write_json(snapshot, {"schema_version": 2, "synthetic": True})
    spec = {
        "as_of_date": "2026-08-14",
        "baseline": {
            "account_count": len(accounts),
            "nav_count": NAV_ROWS,
            "position_count": POSITION_ROWS,
            "transaction_count": 0,
        },
        "crosswalk": [
            {
                "external_account_id": f"SYNTHETIC-{index:04d}",
                "internal_account_id": account_id,
            }
            for index, account_id in enumerate(accounts)
        ],
        "current_snapshot": {
            "artifact_id": "web/current-position-snapshot.json",
            "as_of_date": "2026-08-14",
            "expected_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "expected_size_bytes": snapshot.stat().st_size,
            "relative_path": "normalized/schwab-web-current-snapshot.json",
            "root_id": "source-run",
            "schema_name": "schwab-web-current-position-snapshot",
            "schema_version": "2",
        },
        "roots": {
            "har": str(har),
            "legacy": str(legacy),
            "source-run": str(prior_root),
        },
        "schema_version": 2,
        "sources": sources,
    }
    spec_path = prior_root / "prior-spec.json"
    _write_json(spec_path, spec)
    return spec_path


class _Fixture:
    def __init__(self, tmp_path: Path, accounts: list[str]) -> None:
        self.accounts = accounts
        self.run_root = tmp_path / "run"
        self.prior_run_root = tmp_path / "prior"
        self.output_dir = self.run_root / "normalized"
        self.spec_out = tmp_path / "spec" / "schwab-generation-spec.json"
        self.gap_path = Path(f"{self.spec_out}.gaps.json")
        self.run_root.mkdir(parents=True)
        self.prior_run_root.mkdir(parents=True)
        _build_run(self.run_root, accounts)
        self.prior_spec_path = _build_prior(self.prior_run_root, accounts)

    def normalize(self) -> Any:
        return normalize(
            run_root=self.run_root,
            prior_spec_path=self.prior_spec_path,
            prior_run_root=self.prior_run_root,
            output_dir=self.output_dir,
            spec_out=self.spec_out,
        )

    def outputs(self) -> list[Path]:
        arrays = [
            self.output_dir / "wch" / "transactions" / f"{account_id}.json"
            for account_id in self.accounts
        ]
        return [*arrays, self.spec_out, self.gap_path]


def test_flatten_preserves_page_order_and_manifest_counts(tmp_path: Path) -> None:
    accounts = ["account-test-1", "account-test-2"]
    fixture = _Fixture(tmp_path, accounts)
    result = fixture.normalize()
    assert result.transaction_count == 2 * PAGES_PER_ACCOUNT * RECORDS_PER_PAGE
    assert result.account_count == 2
    for account_index, account_id in enumerate(accounts):
        target = fixture.output_dir / "wch" / "transactions" / f"{account_id}.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == PAGES_PER_ACCOUNT * RECORDS_PER_PAGE
        expected = [
            f"SYNTHETIC TRADE A{account_index} P{page_index} R{record_index}"
            for page_index in range(PAGES_PER_ACCOUNT)
            for record_index in range(RECORDS_PER_PAGE)
        ]
        assert [record["description"] for record in data] == expected


def test_hash_mismatch_fails_closed_and_writes_nothing(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, ["account-test-1", "account-test-2"])
    page = fixture.run_root / "pages" / "account-test-1" / "0000.json"
    page.write_bytes(page.read_bytes() + b" ")
    with pytest.raises(RefreshNormalizeError):
        fixture.normalize()
    for target in fixture.outputs():
        assert not target.exists()


def test_missing_required_record_key_fails(tmp_path: Path) -> None:
    accounts = ["account-test-1", "account-test-2"]
    fixture = _Fixture(tmp_path, accounts)
    _build_run(fixture.run_root, accounts, drop_key="effectiveDate")
    with pytest.raises(RefreshNormalizeError, match="missing required keys"):
        fixture.normalize()
    for target in fixture.outputs():
        assert not target.exists()


def test_non_null_terminal_bookmark_fails(tmp_path: Path) -> None:
    accounts = ["account-test-1", "account-test-2"]
    fixture = _Fixture(tmp_path, accounts)
    _build_run(fixture.run_root, accounts, terminal_bookmark={"next": "page-2"})
    with pytest.raises(RefreshNormalizeError, match="bookmark is not null"):
        fixture.normalize()
    for target in fixture.outputs():
        assert not target.exists()


def test_emitted_spec_shape_and_root_rewrites(tmp_path: Path) -> None:
    accounts = [f"account-test-{index}" for index in range(1, 9)]
    fixture = _Fixture(tmp_path, accounts)
    fixture.normalize()
    prior = json.loads(fixture.prior_spec_path.read_text(encoding="utf-8"))
    spec = json.loads(fixture.spec_out.read_text(encoding="utf-8"))
    assert set(spec) == {
        "as_of_date",
        "baseline",
        "crosswalk",
        "current_snapshot",
        "roots",
        "schema_version",
        "sources",
    }
    assert spec["schema_version"] == 2
    assert spec["as_of_date"] == "2026-09-02"
    assert spec["crosswalk"] == prior["crosswalk"]
    assert spec["roots"] == {
        "source-run": str(fixture.run_root),
        "prior-source-run": str(fixture.prior_run_root),
        "har": str(fixture.prior_run_root / "schwab_har"),
        "legacy": str(fixture.prior_run_root / "Strategies_dataSchwab"),
    }
    sources = spec["sources"]
    assert len(sources) == 20
    assert Counter(record["role"] for record in sources) == {
        "WCH_TRANSACTION_JSON": 8,
        "WCH_NAV_CSV": 8,
        "MANUAL_TRANSACTION_JSON_ALL": 1,
        "MANUAL_NAV_CSV_ALL": 1,
        "MANUAL_POSITION_CSV": 1,
        "WORKBOOK_REFERENCE": 1,
    }
    prior_by_artifact = {record["artifact_id"]: record for record in prior["sources"]}
    prior_wch_order = [
        record["account_id"]
        for record in prior["sources"]
        if record["role"] == "WCH_TRANSACTION_JSON"
    ]
    new_wch_order = [
        record["account_id"]
        for record in sources
        if record["role"] == "WCH_TRANSACTION_JSON"
    ]
    assert new_wch_order == prior_wch_order
    for record in sources:
        if record["role"] == "WCH_TRANSACTION_JSON":
            account_id = record["account_id"]
            target = fixture.output_dir / "wch" / "transactions" / f"{account_id}.json"
            payload = target.read_bytes()
            assert record["root_id"] == "source-run"
            assert record["relative_path"] == (
                f"normalized/wch/transactions/{account_id}.json"
            )
            assert record["artifact_id"] == f"wch/transactions/{account_id}.json"
            assert record["schema_name"] == "schwab-wch-transaction-history"
            assert record["as_of_date"] == "2026-09-02"
            assert record["expected_sha256"] == hashlib.sha256(payload).hexdigest()
            assert record["expected_size_bytes"] == len(payload)
            assert record["expected_record_count"] == (
                PAGES_PER_ACCOUNT * RECORDS_PER_PAGE
            )
            continue
        prior_record = prior_by_artifact[record["artifact_id"]]
        expected_root = {
            "har": "har",
            "legacy": "legacy",
            "source-run": "prior-source-run",
        }[prior_record["root_id"]]
        assert record["root_id"] == expected_root
        for key in (
            "expected_sha256",
            "expected_size_bytes",
            "relative_path",
            "as_of_date",
            "schema_name",
            "schema_version",
        ):
            assert record[key] == prior_record[key]
        if record["role"] == "MANUAL_TRANSACTION_JSON_ALL":
            assert (
                record["expected_record_count"] == prior_record["expected_record_count"]
            )
    snapshot = spec["current_snapshot"]
    assert snapshot["root_id"] == "prior-source-run"
    assert snapshot["expected_sha256"] == prior["current_snapshot"]["expected_sha256"]
    assert snapshot["relative_path"] == prior["current_snapshot"]["relative_path"]


def test_create_only_refusal(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, ["account-test-1", "account-test-2"])
    fixture.normalize()
    with pytest.raises(RefreshNormalizeError, match="already exists"):
        fixture.normalize()

    fresh = _Fixture(tmp_path / "fresh", ["account-test-1", "account-test-2"])
    fresh.spec_out.parent.mkdir(parents=True, exist_ok=True)
    fresh.spec_out.write_text("{}", encoding="utf-8")
    with pytest.raises(RefreshNormalizeError, match="already exists"):
        fresh.normalize()
    assert not (fresh.output_dir / "wch").exists()
    assert not fresh.gap_path.exists()


def test_gap_report_emitted(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, ["account-test-1", "account-test-2"])
    result = fixture.normalize()
    assert result.gap_report_path == fixture.gap_path
    report = json.loads(fixture.gap_path.read_text(encoding="utf-8"))
    assert set(report) == {"admission_questions"}
    (question,) = report["admission_questions"]
    assert question["id"] == "SCHWAB_REFRESH_NAV_POSITION_TAIL"
    assert question["transaction_as_of"] == "2026-09-02"
    assert question["nav_position_as_of"] == "2026-08-14"
    assert "20260902" in question["note"]
    assert "20260814" in question["note"]


def test_baseline_counts_computed(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, ["account-test-1", "account-test-2"])
    result = fixture.normalize()
    assert result.nav_count == NAV_ROWS
    assert result.position_count == POSITION_ROWS
    spec = json.loads(fixture.spec_out.read_text(encoding="utf-8"))
    assert spec["baseline"] == {
        "account_count": 2,
        "nav_count": NAV_ROWS,
        "position_count": POSITION_ROWS,
        "transaction_count": 2 * PAGES_PER_ACCOUNT * RECORDS_PER_PAGE,
    }


def test_cli_success_and_failure(tmp_path: Path) -> None:
    from tools.schwab_refresh_normalize import main

    fixture = _Fixture(tmp_path, ["account-test-1", "account-test-2"])
    argv = [
        "--run-root",
        str(fixture.run_root),
        "--prior-spec",
        str(fixture.prior_spec_path),
        "--prior-run-root",
        str(fixture.prior_run_root),
        "--output-dir",
        str(fixture.output_dir),
        "--spec-out",
        str(fixture.spec_out),
    ]
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert main(argv) == 0
    summary = json.loads(stdout.getvalue())
    assert summary["transaction_count"] == 2 * PAGES_PER_ACCOUNT * RECORDS_PER_PAGE
    for account_id in fixture.accounts:
        assert account_id not in stdout.getvalue()
    with contextlib.redirect_stderr(io.StringIO()):
        assert main(argv) == 1
