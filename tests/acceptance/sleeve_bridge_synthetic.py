"""SYNTHETIC inputs for the sleeve-bridge tests (#624): invented sleeve, model and trades.

One live sleeve (``acct-parent-sa``) over five NAV days, a model book export, a tag map
and Flex-like executions.  Expected terms are computed in the tests with exact fractions.

Daily components of the sleeve (price result, dividends, commissions, allocated cost):

    2026-02-02  price  -8  div 0  comm -2   alloc +2   (opening day, outside the window)
    2026-02-03  price 100  div 2  comm -3
    2026-02-04  price 110  div 0  comm -1   transfer in 500 (start-of-day capital 1591)
    2026-02-05  price 165  div 0  comm -6
    2026-02-06  price -170 div 0  comm -5
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
import io
import json
from typing import Any

import sleeve_basis_synthetic as base

from acceptance.validate import sleeve_bridge

DECLARED = base.DECLARED
SLEEVE_ID = "sa"

PRICE = {
    "2026-02-02": -8,
    "2026-02-03": 100,
    "2026-02-04": 110,
    "2026-02-05": 165,
    "2026-02-06": -170,
}
DIVIDEND = {"2026-02-03": 2}
COMMISSION = {
    "2026-02-02": -2,
    "2026-02-03": -3,
    "2026-02-04": -1,
    "2026-02-05": -6,
    "2026-02-06": -5,
}
# NAV after allocated costs, by day
NAV = {
    "2026-02-02": 992,
    "2026-02-03": 1091,
    "2026-02-04": 1700,
    "2026-02-05": 1859,
    "2026-02-06": 1684,
}
INFLOW = {"2026-02-02": 1000, "2026-02-04": 500}


def facts() -> dict[str, Any]:
    document = base.facts()
    entries = [
        (
            day,
            PRICE[day] + DIVIDEND.get(day, 0) + COMMISSION[day],
            INFLOW.get(day, 0),
            0,
        )
        for day in base.DAYS
    ]
    rows = base._rows(
        [
            (day, pnl, inflow, 0, 2 if day == base.FIRST else 0)
            for day, pnl, inflow, _ in entries
        ]
    )
    for row in rows:
        day = row["day"]
        row["price_pnl"] = str(PRICE[day])
        row["dividend"] = str(DIVIDEND.get(day, 0))
        row["commission"] = str(COMMISSION[day])
        row["exposure"] = "1000"
    sleeve = next(s for s in document["sleeves"] if s["strategy_id"] == DECLARED)
    sleeve["days"] = rows
    sleeve["sleeve_id"] = SLEEVE_ID
    document["allocated_cost"] = {
        "convention": "EXPOSURE_SHARE",
        "account_costs": [
            {
                "day": "2026-02-02",
                "interest": "1.5",
                "fees": "0.5",
                "dividend_accrual": "0",
            }
        ],
    }
    document["disclosures"] = {
        "manual_assignments": [
            {
                "day": "2026-02-05",
                "how": "assigned",
                "quantity": "-0.5",
                "sleeves": "sa",
            },
            {
                "day": "2026-02-06",
                "how": "split",
                "quantity": "-0.25",
                "sleeves": "sa,sb",
            },
        ]
    }
    return document


def tag_map() -> dict[str, Any]:
    return {
        "parents": [
            {
                "parent_account_id": base.PARENT,
                "sleeves": [
                    {
                        "sleeve_id": SLEEVE_ID,
                        "strategy_id": DECLARED,
                        "effective_start": "2026-02-02",
                        "effective_end": None,
                        "tags": [{"match": "startswith", "pattern": "ModelAlpha"}],
                    },
                    {
                        "sleeve_id": "sb",
                        "strategy_id": "synthetic-other",
                        "effective_start": "2026-02-02",
                        "effective_end": None,
                        "tags": [{"match": "startswith", "pattern": "ModelBeta"}],
                    },
                ],
            }
        ]
    }


def _trade(
    number: int, day: str, reference: str, commission: str, quantity: str = "10"
) -> sleeve_bridge.FlexTrade:
    return sleeve_bridge.FlexTrade(
        transaction_id=str(number),
        day=date.fromisoformat(day),
        order_reference=reference,
        commission=Decimal(commission),
        quantity=Decimal(quantity),
    )


def trades() -> list[sleeve_bridge.FlexTrade]:
    return [
        _trade(1, "2026-02-02", "11-ModelAlphaX", "-2"),
        _trade(2, "2026-02-03", "12-ModelAlphaX", "-1"),
        _trade(3, "2026-02-03", "13-ModelAlphaY", "-2"),
        _trade(4, "2026-02-03", "14-ModelBetaZ", "-9"),  # another sleeve's
        _trade(5, "2026-02-04", "15-ModelAlphaX", "-1"),
        _trade(6, "2026-02-05", "16-ModelAlphaX", "-5.5"),
        _trade(7, "2026-02-05", "", "-0.5", "-0.5"),  # no reference: manual assignment
        _trade(8, "2026-02-05", "", "-0.7", "-3"),  # no reference, not ours
        _trade(9, "2026-02-06", "17-ModelAlphaX", "-4.5"),
        _trade(10, "2026-02-06", "", "-0.5", "-0.25"),  # shared: the facts' split share
    ]


def flex_xml(account: str = "U-SYNTHETIC") -> bytes:
    """The same executions as a Flex statement (plus another account's and a repeat)."""

    lines = ["<FlexQueryResponse><FlexStatements><FlexStatement><Trades>"]
    for trade in trades():
        lines.append(
            f'<Trade accountId="{account}" transactionID="{trade.transaction_id}" '
            f'tradeDate="{trade.day:%Y%m%d}" orderReference="{trade.order_reference}" '
            f'ibCommission="{trade.commission}" ibCommissionCurrency="USD" '
            f'quantity="{trade.quantity}"/>'
        )
    lines.append(
        '<Trade accountId="U-OTHER" transactionID="99" tradeDate="20260203" '
        'orderReference="1-ModelAlphaX" ibCommission="-50" ibCommissionCurrency="USD" '
        'quantity="1"/>'
    )
    first = trades()[0]
    lines.append(
        f'<Trade accountId="{account}" transactionID="{first.transaction_id}" '
        f'tradeDate="{first.day:%Y%m%d}" orderReference="{first.order_reference}" '
        f'ibCommission="{first.commission}" ibCommissionCurrency="USD" '
        f'quantity="{first.quantity}"/>'
    )
    lines.append("</Trades></FlexStatement></FlexStatements></FlexQueryResponse>")
    return "".join(lines).encode()


def model_csv(
    *, change: dict[str, str] | None = None, drop_last: bool = False
) -> bytes:
    """A model book export: equity 1000 -> 1010 -> 1015.05 -> 1104.8995 (100 added) -> ..."""

    equity = {
        "2026-02-02": ("1,000.00", "0"),
        "2026-02-03": ("1,010.00", "0"),
        "2026-02-04": ("1,015.05", "0"),
        "2026-02-05": ("1,104.8995", "100"),
        "2026-02-06": ("1,024.99749", "0"),
    }
    if change:
        for day, value in change.items():
            equity[day] = (value, equity[day][1])
    days = sorted(equity)
    if drop_last:
        days = days[:-1]
    out = io.StringIO()
    out.write("Date,Cash,Cash Added,Total Equity\n")
    for day in days:
        out.write(f'{day},"1.00","{equity[day][1]}","{equity[day][0]}"\n')
    return out.getvalue().encode()


def published(terms: sleeve_bridge.BridgeTerms) -> dict[str, Any]:
    """A contract strategy entry stating exactly the derived terms (producer shape)."""

    return {
        "strategy_id": terms.strategy_id,
        "basis": "SLEEVE_EPOCH",
        "observed_start": terms.window_start.isoformat(),
        "observed_end": terms.window_end.isoformat(),
        "bridge": {
            "account_basis": "SLEEVE_EPOCH",
            "account_return": float(terms.sleeve_return),
            "model_return": float(terms.model_return),
            "gap_pp": float(terms.gap_pp),
            "categories": [
                {"category": name, "effect_pp": float(terms.terms_pp[name])}
                for name in sleeve_bridge.CATEGORIES
            ],
        },
        "model_leg": {
            "compared_observations": terms.observations,
            "source_sha256": terms.model_sha256,
        },
        "cost_usd": {
            "commissions": float(terms.commissions_usd),
            "allocated_interest_fees_and_dividend_accrual": float(terms.allocated_usd),
        },
    }


def deep(value: Any) -> Any:
    return copy.deepcopy(json.loads(json.dumps(value)))
