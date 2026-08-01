from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path
from typing import Any, cast

import pytest

from quant_engine.alpaca import (
    AlpacaGenerationError,
    build_generation,
    create_plan,
    cutover_source,
    publish_candidate,
    stage_candidate,
    verify_published,
)


ACCOUNT_HEADER = [
    "id",
    "currency",
    "status",
    "equity",
    "portfolio_value",
    "buying_power",
    "cash",
    "balance_asof",
]
ORDER_HEADER = [
    "id",
    "client_order_id",
    "created_at",
    "filled_at",
    "filled_avg_price",
    "filled_qty",
    "order_type",
    "qty",
    "side",
    "status",
    "symbol",
    "time_in_force",
]


def _csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _account_row() -> list[str]:
    return [
        "synthetic-provider-account",
        "USD",
        "ACTIVE",
        "100",
        "100",
        "200",
        "25",
        "2026-07-16",
    ]


def _source(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "source"
    data = tmp_path / "dataAlpaca"
    data.mkdir()
    first = root / "alpaca_export_20260717_143321"
    second = root / "alpaca_export_20260717_143432"
    third = root / "alpaca_export_20260717_143530"
    fourth = root / "alpaca_export_20260717_143709"

    _csv(first / "account_info.csv", ["", "Value"], [["status", "ACTIVE"]])
    (first / "account_info.json").write_text('{"id":', encoding="utf-8")
    for name in ("portfolio_history_1W_1D.csv", "portfolio_history_3M_1D.csv"):
        _csv(
            first / name,
            ["timestamp", "equity", "profit_loss", "profit_loss_pct"],
            [["1784246400", "100", "0", "0"]],
        )
    _csv(
        first / "watchlists_metadata.csv",
        ["id", "account_id", "name", "created_at", "updated_at"],
        [["watchlist-1", "synthetic-provider-account", "SYNTH", "", ""]],
    )

    _csv(second / "account_info.csv", ACCOUNT_HEADER, [_account_row()])
    (second / "account_info.json").write_text(
        json.dumps({"id": "synthetic-provider-account"}), encoding="utf-8"
    )
    _csv(
        third / "historical_data.csv",
        [
            "symbol",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
        ],
        [["SYNTH", "2026-07-17T00:00:00Z", "1", "2", "1", "2", "10", "1.0", "1.5"]],
    )

    _csv(fourth / "account.csv", ACCOUNT_HEADER, [_account_row()])
    (fourth / "account.json").write_text(
        json.dumps({"id": "synthetic-provider-account"}), encoding="utf-8"
    )
    _csv(
        fourth / "orders.csv",
        ORDER_HEADER,
        [
            [
                "order-1",
                "client-1",
                "2025-11-24T01:00:00Z",
                "2025-11-24T02:00:00Z",
                "10",
                "2",
                "market",
                "2",
                "buy",
                "filled",
                "SYNTH",
                "day",
            ],
            [
                "order-2",
                "client-2",
                "2025-11-24T03:00:00Z",
                "",
                "",
                "",
                "limit",
                "1",
                "sell",
                "canceled",
                "SYNTH",
                "day",
            ],
            [
                "order-3",
                "client-3",
                "2025-11-24T04:00:00Z",
                "",
                "",
                "",
                "limit",
                "1",
                "buy",
                "rejected",
                "SYNTH",
                "day",
            ],
        ],
    )
    for name in (
        "portfolio_history_1M.csv",
        "portfolio_history_1W.csv",
        "portfolio_history_3M.csv",
    ):
        _csv(
            fourth / name,
            ["timestamp", "equity", "profit_loss", "profit_loss_pct", "period"],
            [["1784246400", "100", "0", "0", "1D"]],
        )
    _csv(
        fourth / "watchlists.csv",
        ["id", "account_id", "created_at", "updated_at", "name"],
        [["watchlist-1", "synthetic-provider-account", "", "", "SYNTH"]],
    )

    code = root / "code"
    code.mkdir(parents=True)
    for name in (
        "alpaca_export.py",
        "alpaca_export_final.py",
        "export_all_alpaca.py",
        "get_all_activities.py",
        "minimal_export.py",
        "simple_export.py",
        "test_auth.py",
    ):
        (code / name).write_text("# synthetic legacy prototype\n", encoding="utf-8")
    (code / "requirements.txt").write_text("synthetic==1\n", encoding="utf-8")

    tracker = tmp_path / "alpaca_events.csv"
    _csv(
        tracker,
        ["date", "type", "symbol", "qty", "price", "cash_after", "nav_after"],
        [
            ["2025-11-24", "buy_fill", "SYNTH", "2", "10", "", ""],
            ["2025-09-27", "deposit", "", "", "", "", ""],
            ["2025-11-25", "withdrawal_inferred", "", "", "", "", ""],
        ],
    )
    return root, data, tracker


def _plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    source, data, tracker = _source(tmp_path)
    result = create_plan(
        source,
        data,
        tracker,
        internal_account_id="account-0000000000000001",
        as_of_date=date(2026, 7, 17),
    )
    return Path(str(result["plan_path"])), source, data


def test_generation_is_deterministic_reconciled_and_gap_explicit(
    tmp_path: Path,
) -> None:
    plan, _, _ = _plan(tmp_path)
    first, first_manifest, first_bytes = build_generation(plan)
    second, second_manifest, second_bytes = build_generation(plan)
    catalog = cast(dict[str, Any], first)
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first_manifest.coverage.status.value == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert len(catalog["orders"]) == 3
    assert len(catalog["transactions"]) == 1
    assert len(catalog["nav_observations"]) == 1
    assert catalog["reconciliation"]["tracker_fill_difference_count"] == 0
    assert catalog["reconciliation"]["tracker_non_order_disposition"] == {
        "deposit": "TRACKER_ONLY_NOT_PROVIDER_ACTIVITY",
        "withdrawal_inferred": "INFERRED_TRACKER_EVIDENCE",
    }
    assert catalog["positions"] == []
    assert (
        "account activities were not exported"
        in catalog["coverage"]["out_of_boundary"][0]
    )
    assert "synthetic-provider-account" not in first_bytes.decode()


def test_stage_publish_verify_and_cutover_are_bounded(tmp_path: Path) -> None:
    plan, source, data = _plan(tmp_path)
    staged = stage_candidate(plan)
    assert staged["published"] is False
    assert source.exists()
    published = publish_candidate(plan, str(staged["generation_id"]))
    assert published["published"] is True
    assert (
        verify_published(plan, str(staged["generation_id"]))["state"]
        == "VERIFIED_PUBLISHED"
    )
    result = cutover_source(plan, str(staged["generation_id"]))
    assert result["state"] == "SINGLE_AUTHORITY_CUTOVER_COMPLETE"
    assert not source.exists()
    assert (data / "CURRENT.json").exists()
    assert Path(str(result["recoverable_from"])).is_dir()


def test_source_drift_and_tracker_mismatch_fail_closed(tmp_path: Path) -> None:
    plan, source, _ = _plan(tmp_path)
    (source / "code" / "test_auth.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(AlpacaGenerationError, match="planned artifact differs"):
        build_generation(plan)

    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    source, data, tracker = _source(mismatch)
    rows = list(csv.reader(tracker.open("r", encoding="utf-8", newline="")))
    rows[1][4] = "11"
    with tracker.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    with pytest.raises(AlpacaGenerationError, match="do not reconcile"):
        create_plan(
            source,
            data,
            tracker,
            internal_account_id="account-0000000000000001",
            as_of_date=date(2026, 7, 17),
        )
