from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
MODULE_PATH = TOOLS_ROOT / "portfolio_implementation_reconciliation.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_implementation_reconciliation_aliases", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load implementation reconciler")
reconciliation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconciliation
SPEC.loader.exec_module(reconciliation)


def _write_transactions(path: Path, row: dict[str, object]) -> None:
    pq.write_table(pa.Table.from_pylist([row]), path)


@pytest.mark.parametrize(
    ("aliases", "expected"),
    [
        (
            {"TRADE": "BUY", "Ticker": "MSFT", "Tot Fees": "0.25"},
            reconciliation.TradeEvent(
                date(2026, 1, 5),
                "MSFT",
                "BUY",
                reconciliation.Decimal("10"),
                reconciliation.Decimal("100"),
                reconciliation.Decimal("0.25"),
            ),
        ),
        (
            {"Type": "BUY", "Symbol": "MSFT", "TotFees": "0.25"},
            reconciliation.TradeEvent(
                date(2026, 1, 5),
                "MSFT",
                "BUY",
                reconciliation.Decimal("10"),
                reconciliation.Decimal("100"),
                reconciliation.Decimal("0.25"),
            ),
        ),
    ],
)
def test_p123_transaction_reader_projects_each_real_producer_shape(
    tmp_path: Path,
    aliases: dict[str, str],
    expected: reconciliation.TradeEvent,
) -> None:
    path = tmp_path / "har_transactions.parquet"
    _write_transactions(
        path,
        {
            "Date": "2026-01-05",
            "Price": "100",
            "Shares": "10",
            "_entity_id": "primary-ai",
            **aliases,
        },
    )

    rows = reconciliation._read_p123_transactions(path)

    assert reconciliation._p123_trade_events(
        rows, "primary-ai", accepted_as_of=date(2026, 1, 5)
    ) == [expected]


def test_p123_transaction_reader_rejects_missing_semantic_alias(
    tmp_path: Path,
) -> None:
    path = tmp_path / "har_transactions.parquet"
    _write_transactions(
        path,
        {
            "Date": "2026-01-05",
            "Price": "100",
            "Shares": "10",
            "Symbol": "MSFT",
            "TotFees": "0.25",
            "_entity_id": "primary-ai",
        },
    )

    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match=r"missing supported P123 transaction type column \(Type/TRADE\)",
    ):
        reconciliation._read_p123_transactions(path)


def test_p123_transaction_reader_rejects_conflicting_populated_aliases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "har_transactions.parquet"
    _write_transactions(
        path,
        {
            "Date": "2026-01-05",
            "Price": "100",
            "Shares": "10",
            "Symbol": "MSFT",
            "Ticker": "AAPL",
            "TotFees": "0.25",
            "Type": "BUY",
            "TRADE": "SELL",
            "_entity_id": "primary-ai",
        },
    )

    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="conflicting populated P123 transaction type aliases",
    ):
        reconciliation._read_p123_transactions(path)


def test_p123_transaction_reader_does_not_invent_a_missing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("table projection must not run after schema lookup fails")

    monkeypatch.setattr(reconciliation.parquet, "read_table", _unexpected_read)
    with pytest.raises(FileNotFoundError):
        reconciliation._read_p123_transactions(
            tmp_path / "missing-har-transactions.parquet"
        )
