"""The model-to-sleeve implementation bridge (#624, option B), on synthetic facts.

The sleeve facts here are built day by day from a price result, commissions, dividends,
allocated costs and one mid-window inflow, so every bridge term has an independent
hand computation.  Real R2 bytes are tested only when the environment names them.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

import portfolio_implementation_sleeve_basis as sbm
import portfolio_report as reporter
import strategy_basis

STRATEGY = "strategy-x"
PARENT = "acct-p"
SLEEVE_ACCOUNT = f"{PARENT}-s1"
DECLARATION = json.dumps(
    {
        "schema": "strategy-basis-declaration/1",
        "strategies": [{"strategy_id": STRATEGY, "basis": "SLEEVE_EPOCH"}],
    }
).encode()


RECEIPT = json.dumps(
    {
        "dispositions": [
            {
                "action": "EXCLUDE",
                "authority_ref": "contract:abc#segment;issue:303",
                "data_class": "PROVIDER_LIVE_RETURN",
                "item_kind": "PRODUCT_DATA_CLASS",
                "materialized_record_count": 0,
                "policy_effective_start": "2026-08-14",
                "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
                "policy_owner": "PRODUCT_OWNER_GOVERNING_ISSUE_303",
                "provider_id": "portfolio123",
                "reason": "NO_CURRENT_PRODUCT_CONSUMER",
            }
        ]
    }
).encode()


def _weekdays(start: date, count: int) -> list[str]:
    days: list[str] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


# (price result, commission, dividend, allocated cost/credit, inflow) per day after the
# opening day; the opening day funds the sleeve with 1000 and earns nothing
_DAILY = [
    ("12", "-1.5", "0", "0.2", "0"),
    ("-8", "0", "0.5", "-0.1", "0"),
    ("15", "-2", "0", "0.1", "300"),
    ("-20", "-1", "0", "0", "0"),
    ("9", "0", "1", "0.3", "0"),
    ("6", "-0.5", "0", "-0.2", "200"),
    ("-4", "0", "0", "0.1", "0"),
    ("11", "-1", "0.4", "0", "0"),
]
_OPENING = Decimal("1000")


def _facts(
    *, daily: list[tuple[str, str, str, str, str]] | None = None
) -> tuple[dict[str, Any], list[str]]:
    daily = daily if daily is not None else _DAILY
    days = _weekdays(date(2026, 3, 2), len(daily) + 1)
    rows = [
        {
            "day": days[0],
            "event": "IN",
            "nav_end": str(_OPENING),
            "nav_end_after_alloc": str(_OPENING),
            "price_pnl": "0",
            "commission": "0",
            "dividend": "0",
            "alloc_total": "0",
        }
    ]
    flows = [
        {
            "account_id": SLEEVE_ACCOUNT,
            "amount": str(_OPENING),
            "class": "INTERNAL_IN",
            "event_date": days[0],
            "nav_recognition_date": days[0],
            "pair_id": f"{days[0]}:0001",
            "flow_type": "TRANSFER:INTERNAL:SLEEVE",
        }
    ]
    nav = _OPENING
    cumulative = Decimal(0)
    for index, (price, commission, dividend, allocation, inflow) in enumerate(
        daily, start=1
    ):
        day = days[index]
        nav += (
            Decimal(price) + Decimal(commission) + Decimal(dividend) + Decimal(inflow)
        )
        cumulative += Decimal(allocation)
        rows.append(
            {
                "day": day,
                "event": "IN" if Decimal(inflow) else "",
                "nav_end": str(nav),
                "nav_end_after_alloc": str(nav + cumulative),
                "price_pnl": price,
                "commission": commission,
                "dividend": dividend,
                "alloc_total": allocation,
            }
        )
        if Decimal(inflow):
            flows.append(
                {
                    "account_id": SLEEVE_ACCOUNT,
                    "amount": inflow,
                    "class": "INTERNAL_IN",
                    "event_date": day,
                    "nav_recognition_date": day,
                    "pair_id": f"{day}:0001",
                    "flow_type": "TRANSFER:INTERNAL:SLEEVE",
                }
            )
    document = {
        "artifact_id": "sleeve-account-facts",
        "bindings": {"tag_map_sha256": "0" * 64},
        "disclosures": {"manual_assignments": []},
        "flows": flows,
        "parent_account_id": PARENT,
        "provider_id": "ibkr",
        "sleeves": [
            {
                "account_id": SLEEVE_ACCOUNT,
                "days": rows,
                "first_day": days[0],
                "last_day": days[-1],
                "sleeve_id": "s1",
                "strategy_id": STRATEGY,
            }
        ],
    }
    return document, days


def _model_csv(days: list[str], returns: list[str]) -> bytes:
    """A model book export: equity compounding the given daily returns, one cash add."""

    lines = ["Date,Cash,Total Equity,Cash Added"]
    equity = Decimal("2000000")
    lines.append(f'{days[0]},0,"{equity:,.2f}",0.00')
    for day, value in zip(days[1:], returns, strict=True):
        equity = equity * (1 + Decimal(value))
        lines.append(f'{day},0,"{equity:,.6f}",0.00')
    return ("\n".join(lines) + "\n").encode()


MODEL_RETURNS = [
    "0.011",
    "-0.007",
    "0.013",
    "-0.018",
    "0.008",
    "0.005",
    "-0.003",
    "0.010",
]


def _chain(values: list[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    return wealth - 1.0


def _contract(
    document: dict[str, Any], days: list[str]
) -> tuple[bytes, dict[str, bytes]]:
    sources = {
        "declaration": DECLARATION,
        "sleeve_facts": json.dumps(document, sort_keys=True).encode(),
        "model_book_perf": _model_csv(days, MODEL_RETURNS),
        "retained_attribution": _retained(
            "attribution", "bridge", -0.0446, 0.150, -16.76
        ),
        "retained_reconciliation": _retained(
            "reconciliation", "return_reconciliation", -0.0446, 0.150, None
        ),
        "provider_admission_receipt": RECEIPT,
        "model_provider_id": "portfolio123",
    }
    content = sbm.build_contract(
        **sources,
        filenames={key: f"{key}.bin" for key in sources},
    )
    return content, sources


def _retained(
    kind: str, key: str, account: float, model: float, composition: float | None
) -> bytes:
    comparison: dict[str, Any] = {
        "account_return": account,
        "gap_pp": (account - model) * 100,
        "model_return": model,
        "categories": (
            []
            if composition is None
            else [
                {
                    "category": sbm.COMPOSITION_ACCOUNT_CATEGORY,
                    "effect_pp": composition,
                }
            ]
        ),
        "account_cumulative_return": account,
    }
    row = {
        "strategy_id": STRATEGY,
        "verification": "VERIFIED",
        "observed_start": "2026-01-22",
        "observed_end": "2026-07-24",
        key: comparison,
        "account_leg": {"basis": "BASELINE_ACCOUNT_LIVE", "series_sha256": "a" * 64},
        "return_window_comparison": {
            "status": "EXPANDED_BEYOND_PRIOR_DECLARED_OVERLAP"
        },
        "execution_shortfall": {"fill_price_effect_pp": 0.22},
    }
    return json.dumps(
        {
            "attribution_id": "b" * 64,
            "schema_version": 1,
            "strategies": [row],
        },
        sort_keys=True,
    ).encode()


def _block(content: bytes) -> dict[str, Any]:
    return json.loads(content)["strategies"][0]  # type: ignore[no-any-return]


def test_model_returns_net_out_cash_added() -> None:
    raw = (
        b"Date,Total Equity,Cash Added\n"
        b'2026-03-02,"1,000.00",0\n'
        b'2026-03-03,"1,110.00",100\n'
        b'2026-03-04,"1,010.10",0\n'
    )
    rows = sbm.model_returns_from_book_perf(raw)
    assert [day for day, _ in rows] == ["2026-03-03", "2026-03-04"]
    assert rows[0][1] == Decimal("0.01")
    assert rows[1][1] == Decimal("1010.10") / Decimal("1110.00") - 1


def test_the_bridge_matches_an_independent_computation_and_closes() -> None:
    document, days = _facts()
    content, _ = _contract(document, days)
    block = _block(content)
    bridge = block["bridge"]
    # independent arithmetic: base is the prior closing NAV after allocated costs plus
    # the same-day inflow; the governed return keeps the inflow out of the base
    nav = [float(_OPENING)]
    cumulative = [0.0]
    for price, commission, dividend, allocation, inflow in _DAILY:
        nav.append(
            nav[-1] + float(price) + float(commission) + float(dividend) + float(inflow)
        )
        cumulative.append(cumulative[-1] + float(allocation))
    after = [n + c for n, c in zip(nav, cumulative, strict=True)]
    gross, with_commission, net_start, governed = [], [], [], []
    for index, (price, commission, dividend, allocation, inflow) in enumerate(
        _DAILY, 1
    ):
        opening = after[index - 1]
        base = opening + float(inflow)
        pnl = float(price) + float(dividend)
        gross.append(pnl / base)
        with_commission.append((pnl + float(commission)) / base)
        net_start.append((pnl + float(commission) + float(allocation)) / base)
        governed.append((after[index] - float(inflow)) / opening - 1.0)
    commission_only = with_commission
    model = _chain([float(value) for value in MODEL_RETURNS])
    assert bridge["model_return"] == pytest.approx(model, abs=1e-12)
    assert bridge["account_return"] == pytest.approx(_chain(governed), abs=1e-12)
    expected = {
        sbm.CAT_COMPOSITION: 0.0,
        sbm.CAT_EXECUTION: (_chain(gross) - model) * 100,
        sbm.CAT_COMMISSIONS: (_chain(commission_only) - _chain(gross)) * 100,
        sbm.CAT_ALLOCATED: (_chain(net_start) - _chain(commission_only)) * 100,
        sbm.CAT_TIMING: (_chain(governed) - _chain(net_start)) * 100,
        sbm.CAT_RESIDUAL: 0.0,
    }
    found = {item["category"]: item["effect_pp"] for item in bridge["categories"]}
    assert list(found) == list(sbm.CATEGORY_ORDER)
    for category, value in expected.items():
        assert found[category] == pytest.approx(value, abs=1e-9), category
    # the ruling: terms sum to the gap to 1e-8
    assert math.isclose(sum(found.values()), bridge["gap_pp"], abs_tol=1e-8)
    assert bridge["closure"]["difference_pp"] == pytest.approx(0, abs=1e-8)
    # a flow-free sleeve has no timing term; this one has two inflows, so it has one
    assert found[sbm.CAT_TIMING] != 0.0
    # costs in dollars are the facts' own sums
    assert block["cost_usd"]["commissions"] == pytest.approx(
        sum(float(row[1]) for row in _DAILY)
    )


def test_a_flow_free_costless_sleeve_that_equals_the_model_has_only_zero_terms() -> (
    None
):
    daily = [
        ("10", "0", "0", "0", "0"),
        ("-5", "0", "0", "0", "0"),
        ("7", "0", "0", "0", "0"),
    ]
    document, days = _facts(daily=daily)
    # model returns equal the sleeve's own daily result over its prior NAV
    navs = [Decimal(1000)]
    rates = []
    for price, *_ in daily:
        rates.append(str(Decimal(price) / navs[-1]))
        navs.append(navs[-1] + Decimal(price))
    sources = {
        "declaration": DECLARATION,
        "sleeve_facts": json.dumps(document).encode(),
        "model_book_perf": _model_csv(days, rates),
        "retained_attribution": _retained("attribution", "bridge", 0.0, 0.0, 0.0),
        "retained_reconciliation": _retained(
            "reconciliation", "return_reconciliation", 0.0, 0.0, None
        ),
        "provider_admission_receipt": RECEIPT,
        "model_provider_id": "portfolio123",
    }
    block = _block(
        sbm.build_contract(**sources, filenames={key: key for key in sources})
    )
    for item in block["bridge"]["categories"]:
        assert item["effect_pp"] == pytest.approx(0.0, abs=1e-9), item["category"]
    assert block["bridge"]["gap_pp"] == pytest.approx(0.0, abs=1e-9)


def test_verify_rebuilds_the_contract_from_its_sources_and_refuses_a_change() -> None:
    document, days = _facts()
    content, sources = _contract(document, days)
    assert (
        sbm.verify_contract(
            content, **{**sources, "filenames": {k: f"{k}.bin" for k in sources}}
        )["schema"]
        == sbm.SCHEMA
    )
    tampered = {
        **sources,
        "model_book_perf": sources["model_book_perf"].replace(
            b"2,000,000.00", b"2,000,001.00"
        ),
    }
    with pytest.raises(sbm.SleeveBasisError, match="differs from its sources"):
        sbm.verify_contract(
            content, **{**tampered, "filenames": {k: f"{k}.bin" for k in tampered}}
        )


def test_a_sleeve_day_that_does_not_close_to_its_nav_is_refused() -> None:
    document, days = _facts()
    document["sleeves"][0]["days"][3]["price_pnl"] = "99"
    with pytest.raises(sbm.SleeveBasisError, match="does not close"):
        _contract(document, days)


def test_a_model_without_a_sleeve_date_is_refused() -> None:
    document, days = _facts()
    raw = _model_csv(days, MODEL_RETURNS)
    lines = raw.decode().splitlines()
    del lines[4]  # drop one trading day of the model
    sources = {
        "declaration": DECLARATION,
        "sleeve_facts": json.dumps(document).encode(),
        "model_book_perf": ("\n".join(lines) + "\n").encode(),
        "retained_attribution": _retained("attribution", "bridge", 0.0, 0.0, 0.0),
        "retained_reconciliation": _retained(
            "reconciliation", "return_reconciliation", 0.0, 0.0, None
        ),
        "provider_admission_receipt": RECEIPT,
        "model_provider_id": "portfolio123",
    }
    with pytest.raises(sbm.SleeveBasisError, match="model has no return"):
        sbm.build_contract(**sources, filenames={key: key for key in sources})


def _restatement(document: dict[str, Any]) -> strategy_basis.SleeveRestatement:
    return strategy_basis.restatement_from_sleeve_facts(
        json.dumps(document).encode(), STRATEGY
    )


def _validated(
    content: bytes, document: dict[str, Any], sources: dict[str, bytes]
) -> dict[str, Any]:
    payload = json.loads(content)
    return sbm.validate_payload(
        payload,
        {STRATEGY: _restatement(document)},
        retained_attribution_sha256=hashlib.sha256(
            sources["retained_attribution"]
        ).hexdigest(),
        retained_reconciliation_sha256=hashlib.sha256(
            sources["retained_reconciliation"]
        ).hexdigest(),
    )


def test_the_report_side_check_accepts_the_contract_and_rejects_tampering() -> None:
    document, days = _facts()
    content, sources = _contract(document, days)
    assert STRATEGY in _validated(content, document, sources)

    payload = json.loads(content)
    payload["strategies"][0]["bridge"]["categories"][1]["effect_pp"] += 1e-6
    with pytest.raises(sbm.SleeveBasisError, match="does not close"):
        sbm.validate_payload(
            payload,
            {STRATEGY: _restatement(document)},
            retained_attribution_sha256=hashlib.sha256(
                sources["retained_attribution"]
            ).hexdigest(),
            retained_reconciliation_sha256=hashlib.sha256(
                sources["retained_reconciliation"]
            ).hexdigest(),
        )
    payload = json.loads(content)
    payload["strategies"][0]["sleeve_leg"]["series_sha256"] = "0" * 64
    with pytest.raises(sbm.SleeveBasisError, match="sleeve leg differs"):
        sbm.validate_payload(
            payload,
            {STRATEGY: _restatement(document)},
            retained_attribution_sha256=hashlib.sha256(
                sources["retained_attribution"]
            ).hexdigest(),
            retained_reconciliation_sha256=hashlib.sha256(
                sources["retained_reconciliation"]
            ).hexdigest(),
        )
    with pytest.raises(sbm.SleeveBasisError, match="different retained contracts"):
        sbm.validate_payload(
            json.loads(content),
            {STRATEGY: _restatement(document)},
            retained_attribution_sha256="1" * 64,
            retained_reconciliation_sha256=hashlib.sha256(
                sources["retained_reconciliation"]
            ).hexdigest(),
        )
    other = _restatement(_facts(daily=_DAILY[:-1])[0])
    with pytest.raises(sbm.SleeveBasisError, match="sleeve leg differs"):
        sbm.validate_payload(
            json.loads(content),
            {STRATEGY: other},
            retained_attribution_sha256=hashlib.sha256(
                sources["retained_attribution"]
            ).hexdigest(),
            retained_reconciliation_sha256=hashlib.sha256(
                sources["retained_reconciliation"]
            ).hexdigest(),
        )


def _leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _leaves(v, f"{path}/{k}")]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in _leaves(v, f"{path}[{i}]")]
    return [(path, value)]


def test_the_account_basis_rows_stay_visible_under_a_label_and_the_sleeve_is_primary() -> (
    None
):
    document, days = _facts()
    content, sources = _contract(document, days)
    block = _validated(content, document, sources)[STRATEGY]
    attribution_row = json.loads(sources["retained_attribution"])["strategies"][0]
    reconciliation_row = json.loads(sources["retained_reconciliation"])["strategies"][0]
    attribution, reconciled = sbm.apply_to_rows(
        block, attribution_row, reconciliation_row
    )
    assert attribution is not None and reconciled is not None
    assert attribution["basis"] == "SLEEVE_EPOCH"
    assert attribution["bridge"]["account_basis"] == "SLEEVE_EPOCH"
    assert attribution["bridge"]["account_return"] == block["bridge"]["account_return"]
    assert reconciled["return_reconciliation"]["account_basis"] == "SLEEVE_EPOCH"
    # nothing of a retained row is lost: every leaf value of it is in account_basis
    for source, derived in (
        (attribution_row, attribution["account_basis"]),
        (reconciliation_row, reconciled["account_basis"]),
    ):
        assert derived["basis"] == "BASELINE_ACCOUNT_LIVE"
        assert "whole parent account" in derived["basis_label"]
        wanted = [leaf for leaf in _leaves(source) if leaf[0] != "/strategy_id"]
        have = [leaf[1] for leaf in _leaves(derived)]
        # the reconciliation row keeps its event evidence at the top level, outside the sub-block
        remaining = {path: value for path, value in wanted if value not in have}
        carried = (
            {**{p: v for p, v in _leaves(reconciled)}}
            if derived is reconciled["account_basis"]
            else {}
        )
        assert all(value in carried.values() for value in remaining.values()), remaining
    assert attribution["account_basis"]["bridge"]["gap_pp"] == pytest.approx(
        (-0.0446 - 0.150) * 100
    )
    assert (
        reconciled["account_basis"]["return_reconciliation"][
            "account_cumulative_return"
        ]
        == -0.0446
    )


def test_the_report_helper_applies_the_contract_and_refuses_unbound_use(
    tmp_path: Path,
) -> None:
    document, days = _facts()
    content, sources = _contract(document, days)
    path = tmp_path / "implementation-sleeve-basis.json"
    path.write_bytes(content)
    restatements = {STRATEGY: _restatement(document)}
    attribution_row = json.loads(sources["retained_attribution"])["strategies"][0]
    reconciliation_row = json.loads(sources["retained_reconciliation"])["strategies"][0]
    refs = {
        "attribution": {
            "sha256": hashlib.sha256(sources["retained_attribution"]).hexdigest()
        },
        "reconciliation": {
            "sha256": hashlib.sha256(sources["retained_reconciliation"]).hexdigest()
        },
    }
    reconciliation, attribution, ids = reporter._apply_implementation_sleeve_basis(
        path,
        restatements,
        {STRATEGY: dict(reconciliation_row)},
        refs["reconciliation"],
        {STRATEGY: dict(attribution_row)},
        refs["attribution"],
    )
    assert list(ids) == [STRATEGY]
    assert "PROVIDER_ADMIT_OR_DECLARE_V1" in ids[STRATEGY]
    assert "2026-08-14" in ids[STRATEGY]
    assert attribution is not None and reconciliation is not None
    assert attribution[STRATEGY]["bridge"]["status"] == sbm.BRIDGE_STATUS
    assert "account_basis" in attribution[STRATEGY]
    with pytest.raises(reporter.ReportValidationError, match="declaration"):
        reporter._apply_implementation_sleeve_basis(path, {}, None, None, None, None)
    with pytest.raises(reporter.ReportValidationError, match="different retained"):
        reporter._apply_implementation_sleeve_basis(
            path,
            restatements,
            None,
            None,
            {STRATEGY: dict(attribution_row)},
            {"sha256": "2" * 64},
        )
    # the scope marker drops the implementation bridge from the parent-basis list
    report = {
        "strategy_summary": [
            {
                "strategy_id": STRATEGY,
                "return_scope": {
                    "not_restated": [
                        "benchmark_comparison",
                        "implementation_return_attribution",
                    ]
                },
                "return_scope_label": "x, the implementation bridge, dependence",
            }
        ]
    }
    report["methodology"] = []
    reporter._mark_implementation_sleeve_basis(report, ids)
    assert report["methodology"][0]["item"] == (
        "Implementation bridge on the sleeve basis"
    )
    assert "NO_CURRENT_PRODUCT_CONSUMER" in report["methodology"][0]["value"]
    row = report["strategy_summary"][0]
    assert row["return_scope"]["not_restated"] == ["benchmark_comparison"]
    assert "implementation bridge" not in row["return_scope_label"]


def test_the_contract_is_deterministic() -> None:
    document, days = _facts()
    assert _contract(document, days)[0] == _contract(document, days)[0]


# --- real R2 bytes, only when the environment names them ----------------------------

R2_FACTS = os.environ.get("QE_AI624_R2_FACTS")
R2_MODEL = os.environ.get("QE_AI624_R2_MODEL_PERF")
R2_ATTRIBUTION = os.environ.get("QE_AI624_R2_RETAINED_ATTRIBUTION")
R2_RECONCILIATION = os.environ.get("QE_AI624_R2_RETAINED_RECONCILIATION")
R2_RECEIPT = os.environ.get("QE_AI624_R2_ADMISSION_RECEIPT")
R2_FLEX_ROWS = os.environ.get("QE_AI624_R2_FLEX_TRADE_ROWS")


@pytest.mark.skipif(
    not (R2_FACTS and R2_MODEL and R2_ATTRIBUTION and R2_RECONCILIATION and R2_RECEIPT),
    reason="real R2 bytes are named by QE_AI624_R2_FACTS, _MODEL_PERF, _RETAINED_ATTRIBUTION, _RETAINED_RECONCILIATION and _ADMISSION_RECEIPT",
)
def test_real_r2_sleeve_bridge() -> None:
    assert R2_FACTS and R2_MODEL and R2_ATTRIBUTION and R2_RECONCILIATION and R2_RECEIPT
    facts = Path(R2_FACTS).read_bytes()
    model = Path(R2_MODEL).read_bytes()
    attribution = Path(R2_ATTRIBUTION).read_bytes()
    reconciliation = Path(R2_RECONCILIATION).read_bytes()
    declared = json.dumps(
        {
            "schema": "strategy-basis-declaration/1",
            "strategies": [{"strategy_id": "p123-ai-factor", "basis": "SLEEVE_EPOCH"}],
        }
    ).encode()
    content = sbm.build_contract(
        declaration=declared,
        sleeve_facts=facts,
        model_book_perf=model,
        retained_attribution=attribution,
        retained_reconciliation=reconciliation,
        provider_admission_receipt=Path(R2_RECEIPT).read_bytes(),
        model_provider_id="portfolio123",
        filenames={
            "provider_admission_receipt": "x",
            "declaration": "d",
            "sleeve_facts": "f",
            "model_book_perf": "m",
            "retained_attribution": "a",
            "retained_reconciliation": "r",
        },
    )
    block = _block(content)
    bridge = block["bridge"]
    # the model series ties to the retained contract's own model figure over its window
    rows = sbm.model_returns_from_book_perf(model)
    retained = json.loads(attribution)["strategies"][0]["bridge"]
    window = [
        value
        for day, value in rows
        if retained_start(attribution) <= day <= retained_end(attribution)
    ]
    assert float(_decimal_chain(window)) == pytest.approx(
        retained["model_return"], abs=1e-12
    )
    # the sleeve leg is the K series and the closure holds to the ruling
    assert block["sleeve_leg"]["full_series_sha256"] == (
        "4243e0268646c7b542094951ce6b7f6fb5b92d39b041780512bcd196abd04a77"
    )
    assert math.isclose(
        sum(item["effect_pp"] for item in bridge["categories"]),
        bridge["gap_pp"],
        abs_tol=1e-8,
    )
    # X's ADR 0002 end-of-day term over the sleeve window, derived there a second way
    assert block["sleeve_window_extension"][
        "flow_timing_effect_pp_over_full_sleeve_window"
    ] == pytest.approx(-0.3185760344147038, abs=1e-9)
    # the account-basis figures of the committed R2 panel are carried by reference
    reference = block["account_basis_ref"]
    assert reference["account_return"] == pytest.approx(-0.04463063216664376)
    assert reference["gap_pp"] == pytest.approx(-19.468638056906208)
    assert reference["composition_effect_pp"] == pytest.approx(-16.76437666920091)


def retained_start(raw: bytes) -> str:
    return str(json.loads(raw)["strategies"][0]["observed_start"])


def retained_end(raw: bytes) -> str:
    return str(json.loads(raw)["strategies"][0]["observed_end"])


def _decimal_chain(values: list[Decimal]) -> Decimal:
    wealth = Decimal(1)
    for value in values:
        wealth *= 1 + value
    return wealth - 1


# --- order dependence, commission weighting, exclusion disclosure -------------------


def test_the_bridge_names_why_the_ledger_excludes_the_model_returns() -> None:
    document, days = _facts()
    block = _block(_contract(document, days)[0])
    disclosure = block["model_leg"]["admission_disclosure"]
    assert block["model_leg"]["admission"] == sbm.MODEL_ADMISSION
    assert disclosure["policy_id"] == "PROVIDER_ADMIT_OR_DECLARE_V1"
    assert disclosure["policy_effective_start"] == "2026-08-14"
    assert disclosure["authority_ref"].endswith("issue:303")
    assert disclosure["receipt_sha256"] == hashlib.sha256(RECEIPT).hexdigest()
    assert "not ledger admitted" in disclosure["statement"]
    assert "ruling" in disclosure["statement"]
    # an absent exclusion is refused rather than papered over
    with pytest.raises(sbm.SleeveBasisError):
        sbm.exclusion_disclosure(b'{"dispositions": []}', "0" * 64, "portfolio123")


def test_every_ordering_ends_at_the_sleeve_return_and_the_split_depends_on_order() -> (
    None
):
    document, days = _facts()
    sensitivity = _block(_contract(document, days)[0])["order_sensitivity"]
    named = {item["name"]: item for item in sensitivity["named_orderings"]}
    plan = named["PLAN_ORDER_GROSS_THEN_COSTS_THEN_TIMING"]
    block = _block(_contract(document, days)[0])
    found = {
        item["category"]: item["effect_pp"] for item in block["bridge"]["categories"]
    }
    # the plan order is the bridge itself
    for category, value in plan["effects_pp"].items():
        assert found[category] == pytest.approx(value, abs=1e-12)
    gap = block["bridge"]["gap_pp"]
    for item in sensitivity["named_orderings"]:
        # any order telescopes to the same gap, the residual is zero
        assert item["residual_pp"] == pytest.approx(0.0, abs=1e-12)
        assert sum(item["effects_pp"].values()) == pytest.approx(gap, abs=1e-9)
    # the split is order dependent: commissions and timing move with where timing sits
    first = named["TIMING_FIRST"]["effects_pp"]
    assert first[sbm.CAT_TIMING] == 0.0
    spread = sensitivity["admissible_orderings"]
    assert spread["count"] == 8
    assert spread["min_pp"][sbm.CAT_TIMING] < spread["max_pp"][sbm.CAT_TIMING]
    # a cost-only series is not a counterpart of the model: that order is not admissible
    assert sensitivity["all_orderings"]["count"] == 24
    assert named["COMMISSIONS_FIRST"]["effects_pp"][sbm.CAT_EXECUTION] != pytest.approx(
        plan["effects_pp"][sbm.CAT_EXECUTION], abs=1e-3
    )


def test_the_commission_term_is_a_rate_on_the_capital_path_not_dollars_over_one_capital() -> (
    None
):
    document, days = _facts()
    block = _block(_contract(document, days)[0])
    weighting = block["commission_weighting"]
    commissions = [float(row[1]) for row in _DAILY]
    after = [float(_OPENING)]
    inflows = [float(row[4]) for row in _DAILY]
    for price, commission, dividend, allocation, inflow in _DAILY:
        after.append(
            after[-1]
            + float(price)
            + float(commission)
            + float(dividend)
            + float(inflow)
        )
    rates = []
    capital = []
    cum = 0.0
    nav = [float(_OPENING)]
    for index, row in enumerate(_DAILY, 1):
        cum += float(row[3])
        nav.append(after[index] + cum)
    for index, commission in enumerate(commissions, 1):
        base = nav[index - 1] + inflows[index - 1]
        capital.append(base)
        rates.append(commission / base)
    assert weighting["commissions_usd"] == pytest.approx(sum(commissions))
    assert weighting["daily_rate_summed_pp"] == pytest.approx(sum(rates) * 100)
    assert weighting["daily_rate_chained_pp"] == pytest.approx(_chain(rates) * 100)
    assert weighting["capital_mean"] == pytest.approx(sum(capital) / len(capital))
    # dollars over one capital figure is a different number from the chained rate
    assert weighting["usd_over_mean_capital_pp"] != pytest.approx(
        weighting["daily_rate_chained_pp"], abs=1e-3
    )


@pytest.mark.skipif(
    not (R2_FACTS and R2_FLEX_ROWS and R2_MODEL),
    reason="needs QE_AI624_R2_FACTS, _FLEX_TRADE_ROWS (read-only extract of the Flex Trade rows) and _MODEL_PERF",
)
def test_real_flex_trade_rows_reproduce_the_sleeve_commissions_and_fills() -> None:
    """Independent derivation from IBKR Flex Trade rows: commissions counted once.

    The sleeve tag is the one whose first-day executions are the sleeve's entry
    (count and net purchases equal to the facts' entry flow).
    """

    assert R2_FACTS and R2_FLEX_ROWS and R2_MODEL
    facts = json.loads(Path(R2_FACTS).read_bytes())
    extract = json.loads(Path(R2_FLEX_ROWS).read_text())
    sleeve = next(x for x in facts["sleeves"] if x["strategy_id"] == "p123-ai-factor")
    by_day = {d["day"]: d for d in sleeve["days"]}
    entry = by_day[sleeve["first_day"]]
    entry_flow = Decimal(entry["flow_in"])
    tag = next(
        key
        for key, (count, net) in extract["day1_tags"].items()
        if abs(Decimal(net) - entry_flow) < Decimal("0.01")
    )
    import re

    lead = re.compile(r"^[0-9]+-?")
    commission: dict[str, Decimal] = {}
    for row in extract["rows"]:
        if lead.sub("", row["orderReference"]) != tag:
            continue
        day = f"{row['tradeDate'][:4]}-{row['tradeDate'][4:6]}-{row['tradeDate'][6:]}"
        commission[day] = commission.get(day, Decimal(0)) + Decimal(row["ibCommission"])
        # the fill price carries no commission: proceeds are quantity times price
        assert abs(
            abs(Decimal(row["proceeds"]))
            - abs(Decimal(row["quantity"])) * Decimal(row["tradePrice"])
        ) <= Decimal("0.01")
    # (a) the facts commission is the Flex ibCommission, day by day, so the NAV the
    # returns come from holds each commission dollar once; the only days that differ
    # are the days of the facts' declared manual assignments that touch this sleeve
    manual = {
        item["day"]
        for item in facts["disclosures"]["manual_assignments"]
        if "s2" in str(item["sleeves"]).split(",")
    }
    differing = {
        day: Decimal(item["commission"]) - commission.get(day, Decimal(0))
        for day, item in by_day.items()
        if abs(Decimal(item["commission"]) - commission.get(day, Decimal(0)))
        > Decimal("0.0001")
    }
    assert set(differing) <= manual, sorted(set(differing) - manual)
    assert all(abs(value) < Decimal("0.1") for value in differing.values())
    window_total = sum(
        (Decimal(item["commission"]) for item in by_day.values()), Decimal(0)
    )
    assert abs(window_total - sum(commission.values())) <= Decimal("0.2")


@pytest.mark.skipif(
    not (R2_FACTS and R2_FLEX_ROWS and R2_MODEL and R2_ATTRIBUTION),
    reason="needs the R2 facts, the Flex trade-row extract, the model perf export and the retained contract",
)
def test_real_cost_scale_explains_the_commission_and_gross_terms() -> None:
    """(a)-(c) on real bytes: the offsetting pair is a real cost against a model's smaller one."""

    import csv
    import re
    from collections import defaultdict

    assert R2_FACTS and R2_FLEX_ROWS and R2_MODEL
    extract = json.loads(Path(R2_FLEX_ROWS).read_text())
    transactions = Path(R2_MODEL).with_name("strategy_1853536_transactions.csv")
    lead = re.compile(r"^[0-9]+-?")
    tag = max(extract["day1_tags"], key=lambda key: extract["day1_tags"][key][0])
    start, end = "2026-02-02", "2026-09-11"

    def iso(text: str) -> str:
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"

    fills: dict[tuple[str, str, str], list[Decimal]] = defaultdict(
        lambda: [Decimal(0)] * 3
    )
    for row in extract["rows"]:
        day = iso(row["tradeDate"])
        if lead.sub("", row["orderReference"]) != tag or not start <= day <= end:
            continue
        quantity = abs(Decimal(row["quantity"]))
        key = (day, row["symbol"], "BUY" if Decimal(row["quantity"]) > 0 else "SELL")
        fills[key][0] += quantity
        fills[key][1] += quantity * Decimal(row["tradePrice"])
        fills[key][2] += -Decimal(row["ibCommission"])
    model: dict[tuple[str, str, str], list[Decimal]] = defaultdict(
        lambda: [Decimal(0)] * 3
    )
    with transactions.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["Type"] not in ("BUY", "SELL") or not start <= row["Date"] <= end:
                continue
            quantity = abs(Decimal(row["Shares"].replace(",", "")))
            key = (row["Date"], row["Symbol"], row["Type"])
            model[key][0] += quantity
            model[key][1] += quantity * Decimal(row["Price"].replace(",", ""))
            if row["TotFees"]:
                model[key][2] += abs(Decimal(row["TotFees"].replace(",", "")))
    fee_bps_actual = sum(v[2] for v in fills.values()) / sum(
        v[1] for v in fills.values()
    )
    fee_bps_model = sum(v[2] for v in model.values()) / sum(
        v[1] for v in model.values()
    )
    # a sleeve of tens of thousands pays per-ticket commissions on small orders; the model
    # book is charged a fraction of that
    assert fee_bps_actual > 2 * fee_bps_model
    # on same-day matched fills the sleeve filled at better prices than the model assumed
    worse = Decimal(0)
    for key, (mq, mn, _) in model.items():
        if key not in fills:
            continue
        fq, fn, _ = fills[key]
        sign = 1 if key[2] == "BUY" else -1
        worse += sign * (fn / fq - mn / mq) * fq
    assert worse < 0
    # (c) the commission term is a rate on the capital path, not dollars over one capital
    facts = Path(R2_FACTS).read_bytes()
    content = sbm.build_contract(
        declaration=json.dumps(
            {
                "schema": "strategy-basis-declaration/1",
                "strategies": [
                    {"strategy_id": "p123-ai-factor", "basis": "SLEEVE_EPOCH"}
                ],
            }
        ).encode(),
        sleeve_facts=facts,
        model_book_perf=Path(R2_MODEL).read_bytes(),
        retained_attribution=Path(R2_ATTRIBUTION).read_bytes(),
        retained_reconciliation=Path(R2_RECONCILIATION or "").read_bytes(),
        provider_admission_receipt=Path(R2_RECEIPT or "").read_bytes(),
        model_provider_id="portfolio123",
        filenames={
            "declaration": "d",
            "sleeve_facts": "f",
            "model_book_perf": "m",
            "retained_attribution": "a",
            "retained_reconciliation": "r",
            "provider_admission_receipt": "x",
        },
    )
    block = _block(content)
    weighting = block["commission_weighting"]
    found = {item["category"]: item for item in block["bridge"]["categories"]}
    # the same commissions the Flex rows carry, on the facts' window
    assert weighting["commissions_usd"] == pytest.approx(
        -float(sum(v[2] for v in fills.values())), abs=0.2
    )
    # the term is the chained daily rate scaled by the growth the gross result gave the
    # capital it was charged against (the lost dollars would have grown too)
    growth = 1.0 + found[sbm.CAT_EXECUTION]["ending_return"]
    assert found[sbm.CAT_COMMISSIONS]["effect_pp"] == pytest.approx(
        weighting["daily_rate_chained_pp"] * growth, abs=0.02
    )
    # dollars over the mean capital is a smaller number because the dollars were
    # spent when the sleeve was small
    assert abs(weighting["usd_over_mean_capital_pp"]) < abs(
        weighting["daily_rate_chained_pp"]
    )
    assert weighting["capital_last"] > 5 * weighting["capital_first"]
