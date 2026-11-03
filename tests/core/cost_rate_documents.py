"""Synthetic documented-rate evidence rows (Volatis-like and Robuxio-like).

The rates and page numbers mirror the shape of the owner's documentation (a
tiered fee table and a single-rate guide) but every digest here is derived in code,
so no pin is stored.  Tests load this module by path.
"""

from __future__ import annotations

import hashlib
from typing import Any


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def source(artifact: str, page: str, field: str) -> dict[str, str]:
    return {
        "artifact": artifact,
        "field": field,
        "page": page,
        "sha256": digest(artifact),
    }


FACT_SHEET = "Strategies/Fund/Fact Sheet.pdf"
GUIDE = "Strategies/Crypto/Onboarding Guide.pdf"
MEMO = "Strategies/Crypto/Investment Memo.docx"
FEE_TIERS = "matrixport/product_fee_tiers.csv"


def volatis_rates(tier: str = "QUALIFIED_0_1M") -> dict[str, Any]:
    fee_table = [source(FACT_SHEET, "1", "fee table")]
    return {
        "lock": None,
        "placement": "NOT_STATED_IN_DOCUMENTATION",
        "placement_source": None,
        "redemption": None,
        "tier": tier,
        "tier_table": [
            {
                "applies_to": "qualified clients, total AUM up to $1M",
                "label": "Qualified $0-1M",
                "management_rate": "0",
                "performance_mechanism": None,
                "performance_rate": "0.2",
                "sources": fee_table,
                "tier": "QUALIFIED_0_1M",
            },
            {
                "applies_to": "qualified clients, total AUM $1-5M",
                "label": "Qualified $1-5M",
                "management_rate": "0.01",
                "performance_mechanism": None,
                "performance_rate": "0.2",
                "sources": fee_table,
                "tier": "QUALIFIED_1_5M",
            },
            {
                "applies_to": "non-qualified clients",
                "label": "Non-qualified flat",
                "management_rate": "0.035",
                "performance_mechanism": None,
                "performance_rate": None,
                "sources": [source(FACT_SHEET, "2", "non-qualified flat fee")],
                "tier": "NON_QUALIFIED_FLAT",
            },
        ],
    }


def robuxio_rates() -> dict[str, Any]:
    return {
        "lock": {"days": 30, "source": source(GUIDE, "27", "funds locked 30 days")},
        "placement": "INSIDE_VEHICLE_NAV",
        "placement_source": source(GUIDE, "27", "equity curve is NAV, fees deducted"),
        "redemption": {
            "placement": "OUTSIDE_BILLED",
            "rate": "0.005",
            "source": source(FEE_TIERS, "n/a", "redemption fee_rate"),
        },
        "tier": "SINGLE_RATE",
        "tier_table": [
            {
                "applies_to": "single rate, no tiers",
                "label": "Matrixport route",
                "management_rate": "0",
                "performance_mechanism": "over the high-water mark",
                "performance_rate": "0.3",
                "sources": [
                    source(GUIDE, "27", "30% performance fees above the HWM"),
                    source(MEMO, "6", "no management fee"),
                ],
                "tier": "SINGLE_RATE",
            }
        ],
    }


def evidence_row(component: str, rates: dict[str, Any]) -> dict[str, Any]:
    return {"component": component, "cost_class": "PERCENT_RATE", "rates": rates}


# Synthetic fund values (not any account's): the posted fee of each is derived in
# code from the tier row's own annual rate, so no rate or fee is stored here.
_SYNTHETIC_FUND_VALUES = (
    ("2025-01-01", "10201.30"),
    ("2025-02-01", "9724.92"),
    ("2025-03-01", "9383.39"),
    ("2025-04-01", "9425.53"),
    ("2025-05-01", "7416.73"),
    ("2025-06-01", "18214.65"),
)


def volatis_derivation(
    rates: dict[str, Any], tier: str = "NON_QUALIFIED_FLAT"
) -> dict[str, Any]:
    """A posted-fee closure for ``tier``: each fee is the tier annual rate over 12
    times the fund value, rounded to the cent (the rate is read from ``rates``)."""

    from decimal import ROUND_HALF_EVEN, Decimal

    from quant_engine.core.contracts import canonical_json_bytes

    annual = Decimal(
        next(row for row in rates["tier_table"] if row["tier"] == tier)[
            "management_rate"
        ]
    )
    closure = [
        {
            "date": day,
            "fund_value": value,
            "posted_fee": format(
                (annual / 12 * Decimal(value)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_EVEN
                ),
                "f",
            ),
        }
        for day, value in _SYNTHETIC_FUND_VALUES
    ]
    return {
        "closure": closure,
        "closure_sha256": hashlib.sha256(canonical_json_bytes(closure)).hexdigest(),
        "evidence": [
            source(FACT_SHEET, "2", "non-qualified flat fee"),
            source("quantbase/account_activity.csv", "n/a", "PARTNER_SUB_FEE journals"),
        ],
        "hwm": "NONE_BILLED",
        "kind": "POSTED_FEE_CLOSURE",
        "performance_fee": "NONE_BILLED",
        "periods_per_year": 12,
        "tier": tier,
        "tolerance_usd": "0.01",
    }


def rehash(derivation: dict[str, Any]) -> dict[str, Any]:
    """Re-bind ``closure_sha256`` after a test edits the closure rows."""

    from quant_engine.core.contracts import canonical_json_bytes

    derivation["closure_sha256"] = hashlib.sha256(
        canonical_json_bytes(derivation["closure"])
    ).hexdigest()
    return derivation
