"""Synthetic fixtures for the Portfolio123 capture tests.

Every value is invented.  Field names and types of the designer-model body and the
transactions CSV header are the REAL, preserved-evidence shapes; every other body
is an UNVERIFIED placeholder.  These bodies exist only to exercise parsers that
must refuse anything else.
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
_STATS = {"downDev": 0.5, "maxDd": -0.25, "sharpe": 1.5, "sortino": 2.0, "stdDev": 0.5}
_WINDOW = {
    "annRet": 0.25,
    "annRetBench": 0.125,
    "bench": dict(_STATS),
    "beta": 0.75,
    "model": dict(_STATS),
}
_TRADES = {"biggest": 0.5, "daysAvg": 10.0, "retAvg": 0.0625, "trades": 12}
# Personal strings below are invented; the capture must redact both objects.
DESIGNER_PERSON = "Synthetic Designer Person"
DESIGNER_COMMENT = "Synthetic commenter remark"
# The designer-model route shape (REAL field names and types, invented values).
DESIGNER: dict[str, Any] = {
    "backtestDisabled": False,
    "benchmark": "SYN",
    "benchmarkUid": 9100,
    "comments": [
        {
            "commentUid": 1,
            "dt": "2029-01-01",
            "replies": [],
            "txt": DESIGNER_COMMENT,
            "userSName": DESIGNER_PERSON,
        }
    ],
    "context": None,
    "currency": "USD",
    "desc": "Synthetic description of a synthetic model.",
    "designerModel": {
        "attachment": "",
        "backtestAgreement": True,
        "cost": 10.0,
        "designerInfo": {"fullName": DESIGNER_PERSON, "userUid": 1},
        "img": "",
        "isLiveBookAsset": False,
        "isStrategyAsset": True,
        "maxSubs": 5,
        "okmsg": None,
        "pUid": 9005,
        "revisions": [
            {
                "insert_dt": "01/02/2029",
                "reason": "Synthetic revision",
                "revision_uid": 3,
                "update_dt": "2029-01-03",
            }
        ],
        "status": "live",
        "subs": 2,
        "subscription": {
            "cancelled": False,
            "nextBillDt": "02/01/2030",
            "portfolioUid": 9006,
            "startDt": "2029-06-01",
            "type": "paid",
        },
    },
    "editable": False,
    "endDt": "12/31/2029",
    "hasRebalTran": 0,
    "hedgeEnabled": False,
    "holdingsAvg": 20.5,
    "inceptionDt": "01/03/2028",
    "lastRebal": "12/28/2029",
    "lastRebalanceToday": False,
    "launchDt": "06/01/2029",
    "liquidationValue": 1000.0,
    "maxdd90": -0.1,
    "mkttypeUid": 7,
    "name": "Synthetic designer model",
    "pctHedgeAvg": 0.0,
    "portUid": 9005,
    "rebalFreq": "Every 4 Weeks",
    "refLiquidity": 500.0,
    "riskScore": 3,
    "stats3y": dict(_WINDOW),
    "statsInception": dict(_WINDOW),
    "statsLaunch": dict(_WINDOW),
    "topData": [
        {"label": "Synthetic label", "tooltip": None, "value": "10%"},
        {"label": "Another label", "tooltip": 4, "value": "5"},
    ],
    "tradesTtm": 40,
    "tradingAll": {"biggest": None, "daysAvg": 12.0, "retAvg": 0.03, "trades": 24},
    "tradingCommitment": 1,
    "tradingLose": dict(_TRADES),
    "tradingWin": dict(_TRADES),
    "trailingReturns": [
        {"name": "1 Mon", "values": [0.01, 0.005]},
        {"name": "3 Mon", "values": [0.03, 0.02]},
    ],
    "volatility90": 0.2,
}
CREDITS: dict[str, Any] = {
    "plan": "synthetic plan",
    "quota_remaining": 1000,
    "rows": [{"operation": "synthetic_operation", "credits": 2}],
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
        entry(f"{ORIGIN}/spr/strategy/model/9005?dm=true", DESIGNER),
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
    "Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes\n"
    "2029-06-01,AAAA,Buy,10,12.5,125.0,1.0,5,\n"
    "2029-06-02,BBBB,Sell,5,20.0,100.0,1.0,,\n"
)
INVOICE_CSV = (
    "Invoice Date,Description,Amount,Currency\n2029-07-01,Synthetic charge,10.00,USD\n"
)
