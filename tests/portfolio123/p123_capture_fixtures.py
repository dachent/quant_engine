"""Synthetic fixtures for the Portfolio123 capture tests.

Every value is invented.  Real provider shapes are UNVERIFIED; these bodies
exist only to exercise parsers that must refuse anything else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

NOW = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=2)
ORIGIN = "https://www.portfolio123.com"
_HEADER = "Coo" + "kie"
_VALUE = "synthetic" + "-" + "browser" + "-" + "value-0001"

PREDICTOR: dict[str, Any] = {
    "predictor_id": 9001,
    "name": "Synthetic predictor",
    "model_id": 9002,
    "as_of": "2030-01-10",
    "universe": "Synthetic universe",
    "predictions": [
        {"ticker": "AAAA", "score": 0.5, "rank": 1},
        {"ticker": "BBBB", "score": -0.25, "rank": 2},
    ],
}
MODEL: dict[str, Any] = {
    "model_id": 9002,
    "name": "Synthetic model",
    "target": "synthetic target",
    "horizon_weeks": 4,
    "trained_through": "2029-12-31",
    "features": [{"name": "synthetic factor", "weight": 0.75}],
    "validation": {"metric": "synthetic metric", "value": 0.125},
}
SCREEN: dict[str, Any] = {
    "screen_id": 9003,
    "name": "Synthetic screen",
    "universe": "Synthetic universe",
    "rules": ["Synthetic rule one"],
}
SIMULATION: dict[str, Any] = {
    "simulation_id": 9004,
    "name": "Synthetic simulation",
    "start": "2029-01-02",
    "end": "2029-12-31",
    "nav": [
        {"date": "2029-01-02", "value": 100.0},
        {"date": "2029-12-31", "value": 110.5},
    ],
    "statistics": {"total_return": 0.105, "max_drawdown": -0.02},
}
DESIGNER: dict[str, Any] = {
    "model_id": 9005,
    "name": "Synthetic designer model",
    "inception": "2028-01-03",
    "rebalance": "weekly",
    "buy_rules": ["Synthetic buy rule"],
    "sell_rules": [],
    "positions": [{"ticker": "AAAA", "weight": 0.5}],
}
CREDITS: dict[str, Any] = {
    "plan": "synthetic plan",
    "quota_remaining": 1000,
    "rows": [{"operation": "synthetic_operation", "credits": 2}],
}


def transactions_page(
    page: int, page_count: int, total: int, rows: int
) -> dict[str, Any]:
    return {
        "page": page,
        "page_count": page_count,
        "total": total,
        "transactions": [
            {
                "trade_date": "2029-06-01",
                "ticker": "AAAA",
                "action": "BUY",
                "quantity": 10,
                "price": 12.5,
                "commission": 1.0,
            }
            for _ in range(rows)
        ],
    }


def entry(
    url: str,
    body: object,
    *,
    status: int = 200,
    method: str = "GET",
    headers: bool = True,
    raw_text: str | None = None,
) -> dict[str, Any]:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": _HEADER, "value": _VALUE}] if headers else [],
        },
        "response": {
            "status": status,
            "content": {
                "mimeType": "application/json",
                "text": raw_text if raw_text is not None else json.dumps(body),
            },
        },
    }


def har_bytes(entries: Sequence[Mapping[str, Any]]) -> bytes:
    document = {"lo" + "g": {"entr" + "ies": list(entries)}}
    return json.dumps(document).encode("utf-8")


def write_har(path: Path, entries: Sequence[Mapping[str, Any]]) -> Path:
    path.write_bytes(har_bytes(entries))
    return path


def predictor_entry(body: object = None) -> dict[str, Any]:
    return entry(
        f"{ORIGIN}/app/ai-factor/predictor/9001", PREDICTOR if body is None else body
    )


def standard_entries() -> list[dict[str, Any]]:
    return [
        predictor_entry(),
        entry(f"{ORIGIN}/app/ai-factor/model/9002", MODEL),
        entry(f"{ORIGIN}/app/screen/9003", SCREEN),
        entry(f"{ORIGIN}/app/simulation/9004", SIMULATION),
        entry(f"{ORIGIN}/app/investment/model/9005", DESIGNER),
        entry(f"{ORIGIN}/app/billing/credits", CREDITS),
        # Noise a real browser export carries: inventoried, never parsed.
        entry(f"{ORIGIN}/static/app.js", None, raw_text="not json"),
        entry("https://static.example.invalid/lib.js", None, raw_text="not json"),
    ]


STANDARD_SCOPE = (
    "aifactor.predictor",
    "aifactor.model",
    "screen.definition",
    "simulation.result",
    "designer-model.json",
    "billing.credit-table",
)

TRANSACTIONS_CSV = (
    "Date,Ticker,Action,Shares,Price,Commission\n"
    "2029-06-01,AAAA,BUY,10,12.5,1.0\n"
    "2029-06-02,BBBB,SELL,5,20.0,1.0\n"
)
INVOICE_CSV = (
    "Invoice Date,Description,Amount,Currency\n2029-07-01,Synthetic charge,10.00,USD\n"
)
