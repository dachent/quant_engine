"""The independent verifier agrees with the builders and refuses every tampering (#567 stage 4)."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import nested_strategy_tag_map as tag_map_module
import sleeve_ledger as sl
import sleeve_nav as sn
import verify_sleeve_ledger as verifier

_DAYS = ("d1", "d2", "d3", "d4")
_ISO = {"d1": "2026-01-01", "d2": "2026-01-02", "d3": "2026-01-03", "d4": "2026-01-04"}
_PARENT = {
    "tag_normalization": "STRIP_LEADING_DIGITS_AND_DASH",
    "sleeves": [
        {
            "sleeve_id": "s1",
            "effective_start": "2026-01-01",
            "effective_end": None,
            "tags": [{"match": "startswith", "pattern": "BookOne"}],
        },
        {
            "sleeve_id": "s2",
            "effective_start": "2026-01-01",
            "effective_end": None,
            "tags": [{"match": "startswith", "pattern": "BookTwo"}],
        },
    ],
}
_POLICY = {
    "sleeve_ids": ["s1", "s2"],
    "residual_id": "other",
    "aliases": {},
    "unheld_fallback": {},
}


def _inputs() -> dict[str, Any]:
    trades = [
        {
            "day": "d1",
            "timestamp": "1",
            "symbol": "A",
            "quantity": "10",
            "price": "10",
            "close_price": "10",
            "commission": "-1",
            "tag": "11-BookOne",
        },
        {
            "day": "d1",
            "timestamp": "2",
            "symbol": "A",
            "quantity": "20",
            "price": "10",
            "close_price": "10",
            "commission": "0",
            "tag": "BookTwo",
        },
        {
            "day": "d2",
            "timestamp": "1",
            "symbol": "A",
            "quantity": "-15",
            "price": "11",
            "close_price": "11",
            "commission": "-3",
            "tag": None,
        },
        {
            "day": "d3",
            "timestamp": "1",
            "symbol": "B",
            "quantity": "5",
            "price": "20",
            "close_price": "20",
            "commission": "0",
            "tag": None,
        },
    ]
    return {
        "tag_map": deepcopy(_PARENT),
        "day_dates": _ISO,
        "policy": deepcopy(_POLICY),
        "funding": {
            "s1": {
                "d1": {"kind": "IN", "evidence": "DERIVED"},
                "d4": {"kind": "EXIT", "evidence": "DECLARED_UNRESOLVED"},
            },
            "s2": {"d1": {"kind": "IN", "evidence": "DERIVED"}},
        },
        "trades": trades,
        "positions": {
            "d1": {"A": "30"},
            "d2": {"A": "15"},
            "d3": {"A": "15", "B": "5"},
            "d4": {"A": "30", "B": "5"},
        },
        "marks": {
            "d1": {"A": "10"},
            "d2": {"A": "11"},
            "d3": {"A": "11", "B": "20"},
            "d4": {"A": "5.5", "B": "21"},
        },
        "dividends": [{"day": "d3", "symbol": "A", "amount": "3"}],
        "account": [
            {
                "day": d,
                "start_nav": "1000",
                "end_nav": "1004",
                "external_flow": "0",
                "interest": "-1",
                "fees": "-0.5",
                "dividend_accrual": "0.25",
            }
            for d in _DAYS
        ],
        "days": list(_DAYS),
    }


def _publish(inputs: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    parent = tag_map_module.validate_tag_map(
        __import__("json")
        .dumps(
            {
                "schema_version": 1,
                "artifact_id": "nested-strategy-tag-map",
                "as_of_date": "2026-01-04",
                "parents": [
                    {
                        "parent_account_id": "account-parent",
                        "provider_id": "provider-a",
                        "tag_field": "orderReference",
                        "tag_normalization": inputs["tag_map"]["tag_normalization"],
                        "sleeves": [
                            {
                                "sleeve_id": s["sleeve_id"],
                                "strategy_id": f"strategy-{s['sleeve_id']}",
                                "label": s["sleeve_id"],
                                "effective_start": s["effective_start"],
                                "effective_end": s["effective_end"],
                                "tags": s["tags"],
                            }
                            for s in inputs["tag_map"]["sleeves"]
                        ],
                        "unmatched": {"mode": "BLOCK"},
                    }
                ],
            }
        )
        .encode()
    ).parent("account-parent")

    def resolve(tag: str | None, day: str) -> str | None:
        if tag is None:
            return None
        iso = date.fromisoformat(inputs["day_dates"][day])
        found = parent.resolve(tag, iso)
        return None if found is None else found.sleeve_id

    trades = [
        sl.TaggedTrade(
            t["day"],
            t["timestamp"],
            t["symbol"],
            Decimal(t["quantity"]),
            Decimal(t["price"]),
            None if t["close_price"] is None else Decimal(t["close_price"]),
            Decimal(t["commission"]),
            t["tag"],
        )
        for t in inputs["trades"]
    ]
    positions = {
        d: {k: Decimal(v) for k, v in b.items()} for d, b in inputs["positions"].items()
    }
    marks = {
        d: {k: Decimal(v) for k, v in b.items()} for d, b in inputs["marks"].items()
    }
    ledger = sl.derive_sleeve_ledger(
        trades,
        positions,
        marks,
        [
            sl.Dividend(r["day"], r["symbol"], Decimal(r["amount"]))
            for r in inputs["dividends"]
        ],
        resolve,
        sl.SleevePolicy(("s1", "s2"), "other"),
    )
    funding = {
        s: {d: sn.FundingEvent(e["kind"], e["evidence"]) for d, e in events.items()}
        for s, events in inputs["funding"].items()
    }
    account = [
        sn.AccountDay(
            a["day"],
            Decimal(a["start_nav"]),
            Decimal(a["end_nav"]),
            Decimal(a["external_flow"]),
            Decimal(a["interest"]),
            Decimal(a["fees"]),
            Decimal(a["dividend_accrual"]),
        )
        for a in inputs["account"]
    ]
    nav = sn.derive_sleeve_nav(ledger, funding, account, _DAYS, ("s1", "s2"), "other")

    def plain(item: Any) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(item).items()}

    return {
        "ledger_rows": [plain(r) for r in ledger.rows],
        "nav_rows": [plain(r) for r in nav.rows],
        "closure": [plain(r) for r in nav.closure],
    }


def test_the_verifier_agrees_with_the_builders_on_a_rich_scenario() -> None:
    inputs = _inputs()
    published = _publish(inputs)
    assert any(r["event"] == "EXIT" for r in published["nav_rows"])
    assert any(
        r["sleeve_id"] == "other" and r["value"] != "0"
        for r in published["ledger_rows"]
    )
    report = verifier.verify_sleeve_ledger(inputs, published)
    assert report.verified, report.mismatches[:3]
    assert report.compared > 200


@pytest.mark.parametrize(
    "table, index, field",
    [
        ("ledger_rows", 3, "value"),
        ("ledger_rows", 5, "price_pnl"),
        ("nav_rows", 2, "nav_end"),
        ("nav_rows", 1, "alloc_total"),
        ("closure", 2, "unexplained"),
    ],
)
def test_a_changed_published_figure_is_a_mismatch(
    table: str, index: int, field: str
) -> None:
    inputs = _inputs()
    published = _publish(inputs)
    published[table][index][field] = str(
        Decimal(published[table][index][field]) + Decimal("0.01")
    )
    report = verifier.verify_sleeve_ledger(inputs, published)
    assert not report.verified
    assert any(
        m.table == table.replace("_rows", "") and m.field == field
        for m in report.mismatches
    )


def test_a_missing_or_extra_published_row_is_a_mismatch() -> None:
    inputs = _inputs()
    published = _publish(inputs)
    missing = deepcopy(published)
    missing["nav_rows"].pop()
    assert any(
        m.field == "row" and m.published == "absent"
        for m in verifier.verify_sleeve_ledger(inputs, missing).mismatches
    )
    extra = deepcopy(published)
    extra["closure"].append({**extra["closure"][0], "day": "d9"})
    assert any(
        m.field == "row" and m.published == "present"
        for m in verifier.verify_sleeve_ledger(inputs, extra).mismatches
    )


def test_altered_inputs_no_longer_verify_the_published_result() -> None:
    published = _publish(_inputs())
    other_tags = _inputs()
    other_tags["tag_map"]["sleeves"][0]["tags"][0]["pattern"] = "Nothing"
    assert not verifier.verify_sleeve_ledger(other_tags, published).verified
    fewer_trades = _inputs()
    fewer_trades["trades"].pop(0)
    assert not verifier.verify_sleeve_ledger(fewer_trades, published).verified
    other_funding = _inputs()
    other_funding["funding"]["s1"]["d4"]["kind"] = "OUT"
    assert not verifier.verify_sleeve_ledger(other_funding, published).verified


def test_an_ambiguous_tag_map_is_refused_not_verified() -> None:
    inputs = _inputs()
    inputs["tag_map"]["sleeves"][1]["tags"].append(
        {"match": "startswith", "pattern": "BookOne"}
    )
    with pytest.raises(verifier.SleeveVerificationError, match="several sleeves"):
        verifier.verify_sleeve_ledger(
            inputs, {"ledger_rows": [], "nav_rows": [], "closure": []}
        )


def test_the_verifier_shares_no_code_with_the_builders() -> None:
    source = (Path(verifier.__file__)).read_text(encoding="utf-8")
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"sleeve_ledger", "sleeve_nav", "nested_strategy_tag_map"}
