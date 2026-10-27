"""Hand-computed tests for the SPEC-HARNESS sleeve epoch derivation.

Synthetic and hand-checkable: a two-day IBKR sleeve toy (settlement leg), a
tiny Quantbase two-fund toy (opening purchase on the first day, a later
purchase recognised the day after its fill), a toy crosswalk for the
residual-parent / retained-strategy rules, fail-closed input errors, the
unchanged-output rule without --sleeve-inputs, and a determinism check.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date, timedelta
from decimal import Decimal, getcontext, setcontext
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).parents[2] / "acceptance" / "derive_b"
DERIVE_PATH = MODULE_DIR / "derive.py"
CUTOFF = date(2026, 9, 17)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their annotations through sys.modules[__module__]
    sys.modules[spec.name] = module
    # Both modules set the decimal precision at import; keep the ambient
    # context so the load does not change other tests' arithmetic.
    ambient = getcontext().copy()
    try:
        spec.loader.exec_module(module)
    finally:
        setcontext(ambient)
    return module


def make_engine(derivation, sleeves):
    return sleeves.Engine(
        time_weighted_return=derivation.time_weighted_return,
        xirr=derivation.xirr,
        money=derivation.money,
        ratio=derivation.ratio,
        metric=derivation.metric,
        not_derivable_row=lambda metric_id, entity, reason, window=None: {
            "metric_id": metric_id,
            "entity": entity,
            "reason": reason,
            **({"window": window} if window is not None else {}),
        },
    )


def find_metric(output_metrics, metric_id, entity):
    return next(
        row
        for row in output_metrics
        if row["metric_id"] == metric_id and row["entity"] == entity
    )


# ------------------------------------------------------------------ toy tree


def _day_rows(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def _us(day: date) -> str:
    return day.strftime("%m/%d/%Y")


def _dashboard_csv(values: dict[date, str], cost_bases: dict[date, str]) -> str:
    lines = ["time,value,cost_basis,profit_loss"]
    for day in _day_rows(date(2024, 7, 24), CUTOFF):
        lines.append(f"{_us(day)},{values.get(day, '0')},{cost_bases.get(day, '0')},0")
    return "\n".join(lines) + "\n"


def write_toy_tree(root: Path) -> dict[str, str]:
    """Write the full 11-role toy input set under root; return role -> path."""
    d = date
    crisis_values = {
        d(2024, 7, 24): "500",
        d(2024, 7, 25): "510",
        d(2024, 7, 26): "725",
    }
    crisis_basis = {
        d(2024, 7, 24): "500",
        d(2024, 7, 25): "700",
    }
    crisis_basis.update({day: "-25" for day in _day_rows(d(2024, 7, 26), CUTOFF)})
    singleindex_values = {d(2024, 7, 24): "1000", d(2024, 7, 25): "1005"}
    singleindex_basis = {d(2024, 7, 24): "1000"}
    singleindex_basis.update({day: "-5" for day in _day_rows(d(2024, 7, 25), CUTOFF)})
    total_values = {
        day: str(
            Decimal(crisis_values.get(day, "0"))
            + Decimal(singleindex_values.get(day, "0"))
        )
        for day in _day_rows(d(2024, 7, 24), CUTOFF)
    }

    activity_header = (
        "activity_type,id,fund,type,amount,filled,created_to_cover_fees,time,"
        "transaction,executed,filled_at,symbol,created_at,status,date"
    )
    activity_rows = [
        activity_header,
        # crisis (fund 56): opening buy 500 fill 07-24, buy 200 fill 07-25
        # (recognised 07-26), MAX_S -725 fill 07-26 (recognised 07-27).
        "filled_txn,t1,56,BUY,500,Y,False,2024-07-24T15:00:00Z,,,,,,,",
        "order,o1,56,,,,False,,t1,Y,2024-07-24T15:00:00Z,SPY,,,",
        "filled_txn,t2,56,BUY,200,Y,False,2024-07-25T15:00:00Z,,,,,,,",
        "order,o2,56,,,,False,,t2,Y,2024-07-25T15:00:00Z,SPY,,,",
        "filled_txn,t3,56,MAX_S,-725,Y,False,2024-07-26T15:00:00Z,,,,,,,",
        "order,o3,56,,,,False,,t3,Y,2024-07-26T15:00:00Z,SPY,,,",
        # singleindex (fund 14): opening buy 1000 fill 07-24, MAX_S -1005
        # fill 07-25 (recognised 07-26).
        "filled_txn,t4,14,BUY,1000,Y,False,2024-07-24T16:00:00Z,,,,,,,",
        "order,o4,14,,,,False,,t4,Y,2024-07-24T16:00:00Z,UPRO,,,",
        "filled_txn,t5,14,MAX_S,-1005,Y,False,2024-07-25T16:00:00Z,,,,,,,",
        "order,o5,14,,,,False,,t5,Y,2024-07-25T16:00:00Z,UPRO,,,",
    ]

    catalog = {
        "catalog_schema_version": 7,
        "accounts": [
            {
                "account_id": "account-ibkr",
                "base_currency": "USD",
                "provider": "IBKR",
            }
        ],
        "transactions": [
            {
                "account_id": "account-ibkr",
                "kind": "TRADE:ExchTrade:BUY",
                "event_date": "2026-09-16",
                "executed_time": "09:30:00",
                "security_id": "sec-x",
                "quantity": "1",
                "execution_price": "100",
                "close_price": "100",
                "commission": {"currency": "USD", "value": "0"},
                "amount": {"currency": "USD", "value": "-100"},
                "order_reference": "s1tag",
                "record_id": "r1",
            }
        ],
        "positions": [
            {
                "account_id": "account-ibkr",
                "as_of_date": day,
                "security_id": "sec-x",
                "quantity": "1",
                "mark_price": "100",
                "asset_category": "STK",
                "multiplier": "1",
                "record_id": f"p-{day}",
            }
            for day in ("2026-09-16", "2026-09-17")
        ],
        "change_in_nav_observations": [
            {
                "account_id": "account-ibkr",
                "accounting_date": "2026-09-16",
                "opening_nav": "0",
                "ending_nav": "100",
                "components": [
                    {"source_component_key": "depositsWithdrawals", "value": "100"},
                    {"source_component_key": "interest", "value": "0"},
                    {"source_component_key": "otherFees", "value": "0"},
                    {
                        "source_component_key": "changeInDividendAccruals",
                        "value": "0",
                    },
                ],
            },
            {
                "account_id": "account-ibkr",
                "accounting_date": "2026-09-17",
                "opening_nav": "100",
                "ending_nav": "100",
                "components": [
                    {"source_component_key": "depositsWithdrawals", "value": "0"},
                    {"source_component_key": "interest", "value": "0"},
                    {"source_component_key": "otherFees", "value": "0"},
                    {
                        "source_component_key": "changeInDividendAccruals",
                        "value": "0",
                    },
                ],
            },
        ],
        "corporate_actions": [],
    }

    tag_map = {
        "parents": [
            {
                "parent_account_id": "account-ibkr",
                "provider_id": "ibkr",
                "sleeves": [
                    {
                        "sleeve_id": "s1",
                        "strategy_id": "s1-strat",
                        "effective_start": "2026-09-16",
                        "effective_end": None,
                        "tags": [{"match": "exact", "pattern": "s1tag"}],
                    },
                    {
                        "sleeve_id": "s2",
                        "strategy_id": "p123-ai-factor",
                        "effective_start": "2026-09-16",
                        "effective_end": None,
                        "tags": [{"match": "exact", "pattern": "s2tag"}],
                    },
                ],
                "unmatched": {
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s1",
                },
            },
            {
                "parent_account_id": "account-qb",
                "provider_id": "quantbase",
                "sleeves": [
                    {
                        "sleeve_id": "crisis",
                        "strategy_id": "qb-crisis-strat",
                        "effective_start": "2024-07-24",
                        "effective_end": "2024-07-26",
                        "tags": [{"match": "exact", "pattern": "56"}],
                    },
                    {
                        "sleeve_id": "singleindex",
                        "strategy_id": "qb-singleindex-strat",
                        "effective_start": "2024-07-24",
                        "effective_end": "2024-07-25",
                        "tags": [{"match": "exact", "pattern": "14"}],
                    },
                ],
                "unmatched": {"mode": "BLOCK"},
            },
        ]
    }

    ibkr_declared = {
        "parent_account_id": "account-ibkr",
        "residual_id": "other",
        "window": {"first_day": "2026-09-16", "last_day": "2026-09-17"},
        "closure_tolerance": {"per_day": "1.00", "window": "1.00"},
        "change_in_nav_components": {
            "external_flow": ["depositsWithdrawals"],
            "interest": ["interest"],
            "fees": ["otherFees"],
            "dividend_accrual": ["changeInDividendAccruals"],
        },
        "symbol_aliases": {"aliases": {}},
        "unheld_fallback": {},
        "funding_events": {
            "s1": {
                "2026-09-16": {
                    "kind": "IN",
                    "counterparty": "UNALLOCATED_ACCOUNT_CASH",
                }
            }
        },
    }

    qb_declared = {
        "parent_account_id": "account-qb",
        "data_cutoff": CUTOFF.isoformat(),
        "closure_tolerance": {
            "cost_basis": "0.01",
            "nav_total": "0.02",
            "pairing_amount": "0.01",
            "exit_relative": "0.01",
        },
        "pairing": {
            "rule": "EXACT_AMOUNT_DAY_AND_TIME_ELSE_UNALLOCATED",
            "window_seconds": 120,
        },
        "total_dashboard": "total",
        "nav_total_exempt_days": {},
        "funds": {
            "crisis": {
                "fund_id": "56",
                "fund_title": "Crisis Factor Investing",
                "dashboard": "crisis",
                "first_nonzero_day": "2024-07-24",
                "last_nonzero_day": "2024-07-26",
                "opening_tolerance": "0.05",
            },
            "singleindex": {
                "fund_id": "14",
                "fund_title": "Single-Index Leveraged In & Out",
                "dashboard": "singleindex",
                "first_nonzero_day": "2024-07-24",
                "last_nonzero_day": "2024-07-25",
                "opening_tolerance": "0.05",
            },
        },
        "out_of_scope_funds": {
            "107": {
                "fund_title": "Volatis Strategy",
                "dashboard": "volatis",
                "counterparty_account_id": "account-qb",
            }
        },
        "open_items": {
            "funding_attribution": {"treatment": "UNALLOCATED_REMAINDER"},
            "cash_residual": {"amount": "0", "tolerance": "0.01"},
            "dividend_nav_treatment": {"treatment": "NOT_FOLDED_INTO_NAV"},
        },
    }

    crosswalk = {
        "rows": [
            {
                "account_id": "account-ibkr",
                "strategy_id": "p123-ai-factor",
                "effective_start": "2026-01-22",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "account-qb",
                "strategy_id": "volatis-strategy",
                "effective_start": "2024-10-15",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
        ]
    }

    files: dict[str, tuple[str, str]] = {
        "crosswalk": ("toy/crosswalk.json", json.dumps(crosswalk)),
        "ibkr_catalog": ("toy/ibkr-catalog.json", json.dumps(catalog)),
        "tag_map": ("toy/tag-map.json", json.dumps(tag_map)),
        "ibkr_declared_inputs": (
            "toy/ibkr-declared.json",
            json.dumps(ibkr_declared),
        ),
        "quantbase_declared_inputs": (
            "toy/qb-declared.json",
            json.dumps(qb_declared),
        ),
        "quantbase_accounts": (
            "toy/accounts.csv",
            "id,type,settled_balance\nx,alpaca,30\n",
        ),
        "quantbase_account_activity": (
            "toy/account_activity.csv",
            "\n".join(activity_rows) + "\n",
        ),
        "quantbase_dashboard_crisis": (
            "toy/dashboard_crisis.csv",
            _dashboard_csv(crisis_values, crisis_basis),
        ),
        "quantbase_dashboard_singleindex": (
            "toy/dashboard_singleindex.csv",
            _dashboard_csv(singleindex_values, singleindex_basis),
        ),
        "quantbase_dashboard_volatis": (
            "toy/dashboard_volatis.csv",
            _dashboard_csv({}, {}),
        ),
        "quantbase_dashboard_total": (
            "toy/dashboard_total.csv",
            _dashboard_csv(total_values, {}),
        ),
    }
    paths: dict[str, str] = {}
    for role, (relative, text) in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        paths[role] = relative
    return paths


def write_sleeve_inputs(
    root: Path,
    paths: dict[str, str],
    *,
    cutoff: str = CUTOFF.isoformat(),
    drop_roles: tuple[str, ...] = (),
    extra_roles: dict[str, dict[str, str]] | None = None,
    corrupt_roles: tuple[str, ...] = (),
    root_style: str = "qe",
) -> Path:
    entries: dict[str, object] = {}
    for role, relative in paths.items():
        if role in drop_roles:
            continue
        pin = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if role in corrupt_roles:
            pin = "0" * 64
        entries[role] = {"root": root_style, "path": relative, "sha256": pin}
    entries.update(extra_roles or {})
    document = {
        "schema": "qe-cp-sleeve-inputs/1",
        "cutoff": cutoff,
        "inputs": entries,
    }
    path = root / "inputs" / "sleeve-inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1), encoding="utf-8", newline="\n")
    return path


def run_toy(root: Path, sleeves, engine, **overrides):
    paths = write_toy_tree(root)
    selection = write_sleeve_inputs(root, paths, **overrides)
    return sleeves.derive_sleeves(
        sleeve_inputs_path=selection,
        qe_root=root,
        repo_root=root,
        cutoff=CUTOFF,
        engine=engine,
    )


# ------------------------------------------------------- (a) settlement leg


def _toy_ibkr_facts(alloc_per_day: str) -> dict[str, object]:
    """A two-day sleeve: IN 101.5 day 1, EXIT day 2, nav_end_after_alloc 100/0.

    Day 1: nav_end 98.5 + cumulative alloc = 100 reported.  Day 2 (EXIT):
    own_pnl 3, nav_pre 101.5, flow_out -101.5, nav_end 0.  Cumulative
    allocated cost C = 2 * alloc_per_day.
    """
    return {
        "sleeves": {
            "s1": [
                {
                    "day": "2026-01-26",
                    "event": "IN",
                    "nav_end_after_alloc": "100",
                    "alloc_total": alloc_per_day,
                },
                {
                    "day": "2026-01-27",
                    "event": "EXIT",
                    "nav_end_after_alloc": "0",
                    "alloc_total": alloc_per_day,
                },
            ]
        },
        "pairs": [
            {
                "day": "2026-01-26",
                "out_account": "account-p-unallocated",
                "in_account": "account-p-s1",
                "amount": "101.5",
            },
            {
                "day": "2026-01-27",
                "out_account": "account-p-s1",
                "in_account": "account-p-unallocated",
                "amount": "101.5",
            },
        ],
    }


def test_settlement_leg_releases_negative_cumulative_alloc():
    """(a) C = -3: settlement INTERNAL_IN 3 on the exit day closes the epoch."""
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_a")
    sleeves = derivation
    engine = make_engine(derivation, sleeves)
    parent_entry = {
        "parent_account_id": "account-p",
        "sleeves": [{"sleeve_id": "s1", "strategy_id": "strat-s1"}],
    }

    epochs = sleeves.ibkr_epochs(_toy_ibkr_facts("-1.5"), parent_entry)

    assert len(epochs) == 1
    epoch = epochs[0]
    assert epoch.account_id == "account-p-s1"
    assert epoch.effective_start == date(2026, 1, 26)
    assert epoch.effective_end == date(2026, 1, 27)
    assert epoch.nav == [
        (date(2026, 1, 26), Decimal("100")),
        (date(2026, 1, 27), Decimal("0")),
    ]
    settlement = [
        leg
        for leg in epoch.legs
        if leg.cls == "INTERNAL_IN" and leg.recognition_day == date(2026, 1, 27)
    ]
    assert [(leg.amount, leg.event_day) for leg in settlement] == [
        (Decimal("3"), date(2026, 1, 27))
    ]

    metrics, not_derivable, summary = epoch_metrics_of(sleeves, engine, epoch)
    assert not_derivable == []
    assert (
        find_metric(metrics, "capital.epoch.opening", epoch.account_id)["value"]
        == "100.00"
    )
    assert (
        find_metric(metrics, "capital.epoch.net_capital", epoch.account_id)["value"]
        == "1.50"
    )
    assert (
        find_metric(metrics, "capital.epoch.closing", epoch.account_id)["value"]
        == "0.00"
    )
    # Only the exit-day legs are capital flows (the day-1 IN 101.5 leg is
    # inside the opening): -101.5 + 3 = -98.5.
    assert (
        find_metric(metrics, "flows.account.net", epoch.account_id)["value"] == "-98.50"
    )
    assert find_metric(metrics, "flows.account.count", epoch.account_id)["value"] == "2"
    # Hand TWR: (0 - (-98.5)) / 100 - 1 = -0.015.
    assert find_metric(metrics, "twr.account", epoch.account_id)["value"] == (
        "-0.0150000000"
    )
    assert summary["twr"] == Decimal("-0.015")


def test_settlement_leg_positive_cumulative_alloc_is_internal_out():
    """(a, mirror) C = +3: settlement INTERNAL_OUT 3 on the exit day."""
    sleeves = load_module(DERIVE_PATH, "derive_b_for_sleeves_a_mirror")
    parent_entry = {
        "parent_account_id": "account-p",
        "sleeves": [{"sleeve_id": "s1", "strategy_id": "strat-s1"}],
    }

    epochs = sleeves.ibkr_epochs(_toy_ibkr_facts("1.5"), parent_entry)

    settlement = [
        leg
        for leg in epochs[0].legs
        if leg.cls == "INTERNAL_OUT"
        and leg.recognition_day == date(2026, 1, 27)
        and leg.amount == Decimal("3")
    ]
    assert len(settlement) == 1


def epoch_metrics_of(sleeves, engine, epoch):
    return sleeves.epoch_metrics(epoch, engine, CUTOFF, ["toy sha256:0"], "note")


# ------------------------------------------- (b) Quantbase recognition days


def test_quantbase_epoch_flows_start_after_the_first_row(tmp_path: Path):
    """(b) opening purchase is inside the opening; a later purchase is a flow
    recognised the day after its fill day."""
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_b")
    sleeves = derivation
    engine = make_engine(derivation, sleeves)

    result = run_toy(tmp_path, sleeves, engine)

    assert result["sleeve_derivation"]["refusals"] == []
    epochs = {
        block["account_id"]: block for block in result["sleeve_derivation"]["epochs"]
    }
    crisis = epochs["account-qb-crisis"]
    assert crisis["effective_start"] == "2024-07-24"
    assert crisis["effective_end"] == "2024-07-27"
    # Opening purchase (fill 2024-07-24) recognised ON the first row day;
    # the later purchase (fill 2024-07-25) recognised 2024-07-26; the MAX_S
    # (fill 2024-07-26) recognised on the exit day 2024-07-27.
    assert [
        (leg["recognition_day"], leg["event_day"], leg["class"], leg["amount"])
        for leg in crisis["legs"]
    ] == [
        ("2024-07-24", "2024-07-24", "INTERNAL_IN", "500"),
        ("2024-07-26", "2024-07-25", "INTERNAL_IN", "200"),
        ("2024-07-27", "2024-07-26", "INTERNAL_OUT", "725"),
    ]
    metrics = result["metrics"]
    entity = "account-qb-crisis"
    assert find_metric(metrics, "capital.epoch.opening", entity)["value"] == "500.00"
    # capital flows only after the first row: +200 (07-26) and -725 (07-27).
    assert find_metric(metrics, "flows.account.net", entity)["value"] == "-525.00"
    assert find_metric(metrics, "flows.account.count", entity)["value"] == "2"
    assert find_metric(metrics, "capital.epoch.net_capital", entity)["value"] == (
        "-25.00"
    )
    assert find_metric(metrics, "capital.epoch.closing", entity)["value"] == "0.00"
    # Hand TWR: (510/500) * ((725-200)/510) * ((0+725)/725) - 1 = 0.05.
    assert find_metric(metrics, "twr.account", entity)["value"] == "0.0500000000"

    singleindex = epochs["account-qb-singleindex"]
    assert singleindex["effective_end"] == "2024-07-26"
    entity = "account-qb-singleindex"
    assert find_metric(metrics, "twr.account", entity)["value"] == "0.0050000000"
    assert find_metric(metrics, "capital.epoch.net_capital", entity)["value"] == (
        "-5.00"
    )

    ibkr = epochs["account-ibkr-s1"]
    assert ibkr["effective_end"] is None  # open epoch
    assert find_metric(metrics, "date.epoch.end", "account-ibkr-s1")["value"] == (
        "open"
    )
    # The day-1 IN 100 leg is inside the opening: no capital flows at all.
    assert (
        find_metric(metrics, "flows.account.count", "account-ibkr-s1")["value"] == "0"
    )
    # The existing ratio() formatter renders a zero ratio as "0E-10".
    assert find_metric(metrics, "twr.account", "account-ibkr-s1")["value"] == ("0E-10")


# --------------------- (correction) COUNTERPARTY_PAIR retired side


def test_counterparty_pair_retired_side_is_an_epoch_leg(tmp_path: Path):
    """CORRECTION.md: which ACCOUNT a leg sits on decides, not its counterparty.

    Fund 107 (Volatis) re-uses the singleindex MAX_S proceeds within 120 s:
    the two fills form a COUNTERPARTY_PAIR, but the OUT leg sits on the
    singleindex account and IS an epoch leg (it is what takes the epoch's
    NAV to zero); the IN leg sits on fund 107's own account and is not an
    epoch leg of any derived epoch.
    """
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_cp")
    sleeves = derivation
    engine = make_engine(derivation, sleeves)
    paths = write_toy_tree(tmp_path)

    # Fund 107 buys 1005 one minute after the singleindex MAX_S fill
    # (2024-07-25T16:00:00Z): same fill day, within the 120 s pairing
    # window, exact amount -> one COUNTERPARTY_PAIR.
    activity_path = tmp_path / "toy" / "account_activity.csv"
    activity_path.write_text(
        activity_path.read_text(encoding="utf-8")
        + "filled_txn,t6,107,BUY,1005,Y,False,2024-07-25T16:01:00Z,,,,,,,\n"
        + "order,o6,107,,,,False,,t6,Y,2024-07-25T16:01:00Z,VOLX,,,\n",
        encoding="utf-8",
    )
    # Cash closure: net fund flows become 975, so settled cash is -975.
    (tmp_path / "toy" / "accounts.csv").write_text(
        "id,type,settled_balance\nx,alpaca,-975\n", encoding="utf-8"
    )

    selection = write_sleeve_inputs(tmp_path, paths)
    result = sleeves.derive_sleeves(
        sleeve_inputs_path=selection,
        qe_root=tmp_path,
        repo_root=tmp_path,
        cutoff=CUTOFF,
        engine=engine,
    )

    assert result["sleeve_derivation"]["refusals"] == []
    epochs = {
        block["account_id"]: block for block in result["sleeve_derivation"]["epochs"]
    }
    singleindex = epochs["account-qb-singleindex"]
    # The retired side of the COUNTERPARTY_PAIR sits on the singleindex
    # account: it IS an epoch leg (exact decimal strings, unrounded).
    assert [
        (leg["recognition_day"], leg["event_day"], leg["class"], leg["amount"])
        for leg in singleindex["legs"]
    ] == [
        ("2024-07-24", "2024-07-24", "INTERNAL_IN", "1000"),
        ("2024-07-26", "2024-07-25", "INTERNAL_OUT", "1005"),
    ]
    # The other leg of the pair sits on fund 107's own account: no epoch has
    # it (and fund 107's account is never an epoch account).
    every_leg = [leg for block in epochs.values() for leg in block["legs"]]
    assert not any(
        leg["class"] == "INTERNAL_IN" and leg["amount"] == "1005" for leg in every_leg
    )
    # The epoch closes to a zero NAV on its last row through its own legs:
    # opening 1000, the 1005 outflow, closing 0 (a NAV drop to zero with no
    # outflow leg would be a defect, not a result).
    metrics = result["metrics"]
    entity = "account-qb-singleindex"
    assert find_metric(metrics, "capital.epoch.opening", entity)["value"] == ("1000.00")
    assert find_metric(metrics, "capital.epoch.net_capital", entity)["value"] == (
        "-5.00"
    )
    assert find_metric(metrics, "capital.epoch.closing", entity)["value"] == "0.00"
    # Hand TWR: (1005/1000) * ((0+1005)/1005) - 1 = 0.005.
    assert find_metric(metrics, "twr.account", entity)["value"] == "0.0050000000"


# --------------------------------- (c) residual parent / retained strategy


def test_residual_parent_and_retained_strategy_rules():
    """(c) toy crosswalk: residual iff a sleeve strategy equals an INCLUDED
    epoch strategy of the same account; retained = derived minus registry."""
    sleeves = load_module(DERIVE_PATH, "derive_b_for_sleeves_c")
    crosswalk_rows = [
        {
            "account_id": "account-p1",
            "strategy_id": "strat-a",
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        },
        {
            "account_id": "account-p1",
            "strategy_id": "strat-b",
            "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        },
        {
            "account_id": "account-p2",
            "strategy_id": "strat-d",
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        },
    ]
    parents = [
        {
            "parent_account_id": "account-p1",
            "provider_id": "ibkr",
            "sleeves": [
                {"sleeve_id": "s1", "strategy_id": "strat-x"},
                {"sleeve_id": "s2", "strategy_id": "strat-a"},
            ],
        },
        {
            "parent_account_id": "account-p2",
            "provider_id": "quantbase",
            "sleeves": [{"sleeve_id": "crisis", "strategy_id": "strat-c"}],
        },
    ]
    epochs = [
        sleeves.Epoch("account-p1-s1", "strat-x", date(2026, 1, 1), None, [], []),
        sleeves.Epoch(
            "account-p1-s2", "strat-a", date(2026, 1, 1), date(2026, 2, 1), [], []
        ),
        sleeves.Epoch(
            "account-p2-crisis",
            "strat-c",
            date(2024, 7, 24),
            date(2024, 12, 13),
            [],
            [],
        ),
    ]

    universe = sleeves.classify_universe(crosswalk_rows, parents, epochs)

    # p1 is residual (sleeve s2's strat-a is an INCLUDED epoch of p1); p2 is
    # not (its INCLUDED epoch is strat-d, its sleeve is strat-c).
    assert universe["residual_parent_ids"] == ["account-p1"]
    # strat-a is in the baseline crosswalk, so not retained; the EXCLUDED row
    # strat-b still counts toward the registry.
    assert universe["retained_strategy_ids"] == ["strat-c", "strat-x"]
    assert universe["retained_strategy_count"] == 2
    assert universe["registry_strategy_count"] == 5
    assert universe["derived_epoch_ids"] == [
        "account-p1-s1|strat-x|2026-01-01|open",
        "account-p1-s2|strat-a|2026-01-01|2026-02-01",
        "account-p2-crisis|strat-c|2024-07-24|2024-12-13",
    ]
    assert universe["derived_epoch_count"] == 3


# --------------------------------------------------- (d) fail-closed inputs


def test_bad_pin_fails_closed(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d1")
    sleeves = derivation
    with pytest.raises(sleeves.SleeveInputError, match="sha256 mismatch"):
        run_toy(
            tmp_path,
            sleeves,
            make_engine(derivation, sleeves),
            corrupt_roles=("tag_map",),
        )


def test_missing_role_fails_closed(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d2")
    sleeves = derivation
    with pytest.raises(sleeves.SleeveInputError, match="missing role 'crosswalk'"):
        run_toy(
            tmp_path,
            sleeves,
            make_engine(derivation, sleeves),
            drop_roles=("crosswalk",),
        )


def test_unknown_role_fails_closed(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d3")
    sleeves = derivation
    with pytest.raises(sleeves.SleeveInputError, match="unknown role"):
        run_toy(
            tmp_path,
            sleeves,
            make_engine(derivation, sleeves),
            extra_roles={
                "surprise": {
                    "root": "qe",
                    "path": "toy/crosswalk.json",
                    "sha256": "0" * 64,
                }
            },
        )


def test_cutoff_mismatch_fails_closed(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d4")
    sleeves = derivation
    with pytest.raises(sleeves.SleeveInputError, match="differs from the cutoff"):
        run_toy(
            tmp_path,
            sleeves,
            make_engine(derivation, sleeves),
            cutoff="2026-09-18",
        )


def test_declared_window_mismatch_fails_closed(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d5")
    sleeves = derivation
    paths = write_toy_tree(tmp_path)
    declared = json.loads((tmp_path / "toy" / "qb-declared.json").read_text())
    declared["data_cutoff"] = "2026-09-16"
    (tmp_path / "toy" / "qb-declared.json").write_text(json.dumps(declared))
    selection = write_sleeve_inputs(tmp_path, paths)
    with pytest.raises(sleeves.SleeveInputError, match="data_cutoff"):
        sleeves.derive_sleeves(
            sleeve_inputs_path=selection,
            qe_root=tmp_path,
            repo_root=tmp_path,
            cutoff=CUTOFF,
            engine=make_engine(derivation, sleeves),
        )


def test_catalog_schema_below_7_fails_closed(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d6")
    sleeves = derivation
    paths = write_toy_tree(tmp_path)
    catalog = json.loads((tmp_path / "toy" / "ibkr-catalog.json").read_text())
    catalog["catalog_schema_version"] = 6
    (tmp_path / "toy" / "ibkr-catalog.json").write_text(json.dumps(catalog))
    selection = write_sleeve_inputs(tmp_path, paths)
    with pytest.raises(sleeves.SleeveInputError, match="below 7"):
        sleeves.derive_sleeves(
            sleeve_inputs_path=selection,
            qe_root=tmp_path,
            repo_root=tmp_path,
            cutoff=CUTOFF,
            engine=make_engine(derivation, sleeves),
        )


def test_cli_sleeves_only_requires_sleeve_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d7")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "derive.py",
            "--qe-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
            "--sleeves-only",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        derivation.main()
    assert exit_info.value.code == 2


def test_cli_sleeve_input_error_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_d8")
    paths = write_toy_tree(tmp_path)
    selection = write_sleeve_inputs(tmp_path, paths, corrupt_roles=("tag_map",))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "derive.py",
            "--qe-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
            "--sleeve-inputs",
            str(selection),
            "--sleeves-only",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        derivation.main()
    assert exit_info.value.code == 2
    assert "input error:" in capsys.readouterr().err


# --------------------------- (e) unchanged without --sleeve-inputs


def _write_minimal_derivation_inputs(tmp_path: Path, derivation) -> None:
    derivation.LEDGER_PATH = Path("ledger.json")
    derivation.CROSSWALK_PATH = Path("crosswalk.json")
    ledger = {
        "as_of_date": "2026-09-17",
        "nav_observations": [
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-09-16",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-09-17",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
        ],
        "flows": [],
        "capital_anchors": [],
    }
    crosswalk = {
        "as_of_date": "2026-09-17",
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": "account-ibkr",
                "strategy_id": "p123-ai-factor",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            }
        ],
    }
    (tmp_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (tmp_path / "crosswalk.json").write_text(json.dumps(crosswalk), encoding="utf-8")


def test_without_sleeve_inputs_the_output_is_unchanged(tmp_path: Path):
    """(e) the same synthetic inputs as the existing tests: no sleeve key."""
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_e")
    _write_minimal_derivation_inputs(tmp_path, derivation)

    output = derivation.derive(tmp_path)
    again = derivation.derive(tmp_path)

    assert "sleeve_derivation" not in output
    assert json.dumps(output) == json.dumps(again)
    assert find_metric(output["metrics"], "twr.account", "account-ibkr")


def test_with_sleeve_inputs_residual_parent_metrics_are_replaced(tmp_path: Path):
    """The residual parent's four account metrics are not emitted; the sleeve
    metrics and the sleeve_derivation block appear; everything else stays."""
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_e2")
    _write_minimal_derivation_inputs(tmp_path, derivation)
    paths = write_toy_tree(tmp_path)
    selection = write_sleeve_inputs(tmp_path, paths)

    plain = derivation.derive(tmp_path)
    extended = derivation.derive(tmp_path, sleeve_inputs=selection)

    assert "sleeve_derivation" in extended
    replaced = {
        "flows.account.net",
        "flows.account.count",
        "twr.account",
        "mwr.account",
    }
    assert any(
        row["metric_id"] in replaced and row["entity"] == "account-ibkr"
        for row in plain["metrics"]
    )
    assert not any(
        row["metric_id"] in replaced and row["entity"] == "account-ibkr"
        for row in extended["metrics"]
    )
    # every other existing metric is untouched
    assert find_metric(extended["metrics"], "capital.account.cutoff", "account-ibkr")
    assert find_metric(extended["metrics"], "capital.epoch.opening", "account-ibkr-s1")


# ---------------------------------------------------------- (f) determinism


def test_determinism_two_runs_equal_bytes(tmp_path: Path):
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_f")
    sleeves = derivation
    engine = make_engine(derivation, sleeves)

    first = run_toy(tmp_path / "a", sleeves, engine)
    second = run_toy(tmp_path / "b", sleeves, engine)

    def render(result: dict[str, object]) -> str:
        return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=False)

    assert render(first) == render(second)


def test_derive_module_has_no_dynamic_loading_of_its_own():
    """CORRECTION2: the sleeve derivation is inlined; derive.py is one module
    with no importlib by-path loading of its own (the architecture rule)."""
    source = DERIVE_PATH.read_text(encoding="utf-8")
    assert "spec_from_file_location" not in source
    assert "exec_module" not in source
    derivation = load_module(DERIVE_PATH, "derive_b_for_sleeves_g")
    assert callable(derivation.derive_sleeves)
    assert not hasattr(derivation, "_load_sleeves_module")
