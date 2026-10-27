"""Hand-checkable sleeve epoch and fail-closed harness cases."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sys
from decimal import Decimal as D, localcontext
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
from derive_a import cli, derive, numerics, sleeves
from derive_a.sources import InputError
import test_derive_a as original

CUTOFF = dt.date(2026, 9, 17)
FIRST = "2026-09-16"
LAST = "2026-09-17"


def ib_tag() -> dict[str, Any]:
    return {
        "provider_id": "ibkr",
        "parent_account_id": "ib",
        "sleeves": [{"sleeve_id": "s1", "strategy_id": "replaced"}],
    }


def ib_facts(cost: str = "-2") -> dict[str, Any]:
    # Purchase 100, opening NAV 100 + first-day allocated cost, liquidation 100.
    # C=-2 -> receive 2 on exit, so net exit proceeds equal opening 98.
    return {
        "sleeves": {
            "s1": [
                {
                    "day": FIRST,
                    "event": "IN",
                    "nav_end_after_alloc": D(100) + D(cost),
                    "alloc_total": D(cost),
                },
                {
                    "day": LAST,
                    "event": "EXIT",
                    "nav_end_after_alloc": D(0),
                    "alloc_total": D(0),
                },
            ]
        },
        "pairs": [
            {
                "day": FIRST,
                "in_account": "ib-s1",
                "out_account": "ib-unallocated",
                "amount": D(100),
                "kind": "UNALLOCATED_CASH",
            },
            {
                "day": LAST,
                "in_account": "ib-unallocated",
                "out_account": "ib-s1",
                "amount": D(100),
                "kind": "UNALLOCATED_CASH",
            },
        ],
        "closure_summary": {},
        "closure": [],
        "disclosures": {},
        "assumptions": [],
        "refusals": [],
    }


def qb_tag() -> dict[str, Any]:
    return {
        "provider_id": "quantbase",
        "parent_account_id": "qb",
        "sleeves": [{"sleeve_id": "crisis", "strategy_id": "retained"}],
    }


def qb_facts() -> dict[str, Any]:
    return {
        "funds": {
            "crisis": {
                "rows": [
                    {"day": "2026-09-14", "nav_end": D(100)},
                    {"day": "2026-09-15", "nav_end": D(110)},
                    {"day": "2026-09-16", "nav_end": D(170)},
                    {"day": LAST, "nav_end": D(0)},
                ],
                "flows": [
                    {
                        "fill_day": "2026-09-14",
                        "recognition_day": "2026-09-14",
                        "kind": "BUY",
                    },
                    {
                        "fill_day": "2026-09-15",
                        "recognition_day": "2026-09-16",
                        "kind": "BUY",
                    },
                    {
                        "fill_day": "2026-09-16",
                        "recognition_day": LAST,
                        "kind": "MAX_S",
                    },
                ],
            }
        },
        "pairs": [
            {
                "event_day": "2026-09-14",
                "recognition_day": "2026-09-15",
                "in_account": "qb-crisis",
                "out_account": "qb-unallocated",
                "amount": D(100),
                "kind": "UNALLOCATED_CASH",
            },
            {
                "event_day": "2026-09-15",
                "recognition_day": "2026-09-16",
                "in_account": "qb-crisis",
                "out_account": "qb-unallocated",
                "amount": D(50),
                "kind": "UNALLOCATED_CASH",
            },
            {
                "event_day": "2026-09-16",
                "recognition_day": LAST,
                "in_account": "qb-live",
                "out_account": "qb-crisis",
                "amount": D(170),
                "kind": "REALLOCATION",
            },
        ],
        "closure": {},
        "dividends": [],
        "volatis_fills": [],
        "assumptions": [],
        "refusals": [],
    }


@pytest.mark.parametrize("cost,kind", [("-2", "INTERNAL_IN"), ("2", "INTERNAL_OUT")])
def test_settlement_two_day_identity(cost: str, kind: str) -> None:
    (item,) = sleeves.ibkr_epochs(ib_facts(cost), ib_tag())
    (settlement,) = [leg for leg in item["legs"] if leg["kind"] == "SETTLEMENT"]
    assert settlement["class"] == kind
    assert settlement["amount"] == D(2)
    assert settlement["event_day"] == settlement["recognition_day"] == LAST
    assert item["opening"] == D(100) + D(cost)
    assert item["net_capital"] == item["closing"] == 0
    output = derive.Output()
    with localcontext() as context:
        context.prec = numerics.PRECISION
        sleeves.emit_epoch(item, output, CUTOFF, ("toy",))
    metrics = {r["metric_id"]: r for r in output.metrics}
    assert metrics["twr.account"]["value"] == "0.000000000000"
    assert metrics["mwr.account"]["value"] == "0.000000000000"
    assert metrics["flows.account.count"]["value"] == "2"
    assert not output.not_derivable


def test_zero_cost_has_no_settlement() -> None:
    (item,) = sleeves.ibkr_epochs(ib_facts("0"), ib_tag())
    assert len(item["legs"]) == 2


@pytest.mark.parametrize("empty_events", [False, True])
def test_ibkr_unfunded_sleeve_has_no_rows_or_epoch(empty_events: bool) -> None:
    tag = ib_tag()
    tag["sleeves"].append({"sleeve_id": "s2", "strategy_id": "unfunded"})
    for sleeve in tag["sleeves"]:
        sleeve.update(effective_start=FIRST, effective_end=None)
        sleeve["tags"] = [{"match": "exact", "pattern": sleeve["sleeve_id"]}]
    dec: dict[str, Any] = {
        "residual_id": "other",
        "funding_events": {"s1": {FIRST: {"kind": "IN"}}},
        "symbol_aliases": {"aliases": {}},
        "change_in_nav_components": {
            "external_flow": [],
            "interest": [],
            "fees": ["fee"],
            "dividend_accrual": [],
        },
        "closure_tolerance": {"per_day": "0", "window": "0"},
    }
    if empty_events:
        dec["funding_events"]["s2"] = {}
    # Both sleeves hold securities, but only s1 has declared funding. s2's
    # price gain belongs to closure residuals, never the cost-share denominator.
    src: dict[str, Any] = {
        "transactions": [],
        "positions": [],
        "change_in_nav_observations": [],
    }
    for sid in ("s1", "s2"):
        src["transactions"].append(
            {
                "kind": "TRADE:BUY",
                "event_date": FIRST,
                "executed_time": "12:00",
                "security_id": sid,
                "quantity": "1",
                "execution_price": "100",
                "commission": {"value": "0"},
                "amount": {"currency": "USD"},
                "order_reference": sid,
            }
        )
        for day in (FIRST, LAST):
            src["positions"].append(
                {
                    "as_of_date": day,
                    "security_id": sid,
                    "quantity": "1",
                    "mark_price": "110" if sid == "s2" and day == LAST else "100",
                    "asset_category": "STK",
                }
            )
    for day, opening, ending in ((FIRST, "200", "198"), (LAST, "198", "206")):
        src["change_in_nav_observations"].append(
            {
                "accounting_date": day,
                "opening_nav": opening,
                "ending_nav": ending,
                "components": [{"source_component_key": "fee", "value": "-2"}],
            }
        )
    facts = sleeves.ibkr_facts(src, dec, tag)
    assert not facts["sleeves"].get("s2")
    assert set(facts["cumulative"]) == {"s1"}
    assert all(row["exposure_share"] == 1 for row in facts["sleeves"]["s1"])
    assert facts["cumulative"]["s1"]["sum_alloc_total"] == -4
    assert facts["closure"][-1]["residual_and_post_exit"] == 10
    assert all(row["unexplained"] == 0 for row in facts["closure"])
    assert len(facts["pairs"]) == 1
    assert facts["pairs"][0]["in_account"] == "ib-s1"
    assert not facts["refusals"]
    assert len([a for a in facts["assumptions"] if "no declared funding" in a]) == 1
    assert "s2" in facts["assumptions"][-1]
    epochs = sleeves.ibkr_epochs(facts, tag)
    (item,) = epochs
    assert item["account_id"] == "ib-s1"
    assert item["opening"] == 98 and item["closing"] == 96
    output = derive.Output()
    with localcontext() as context:
        context.prec = numerics.PRECISION
        sleeves.emit_epoch(item, output, CUTOFF, ())
    assert {row["entity"] for row in output.metrics} == {"ib-s1"}
    crosswalk = {
        "rows": [
            {
                "account_id": "ib",
                "strategy_id": "unfunded",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        ]
    }
    result = sleeves.universe(crosswalk, [tag], epochs)
    assert result["universe.derived_epoch_count"] == "1"
    assert result["universe.retained_strategy_ids"] == ["replaced"]
    assert result["universe.residual_parent_ids"] == ["ib"]


def test_qb_opening_and_next_day_purchase() -> None:
    assert sleeves.recognition("2026-09-14", "BUY", "2026-09-14") == "2026-09-14"
    assert sleeves.recognition("2026-09-15", "BUY", "2026-09-14") == "2026-09-16"
    (item,) = sleeves.quantbase_epochs(qb_facts(), qb_tag())
    assert item["legs"][0]["recognition_day"] == "2026-09-14"
    assert item["legs"][1]["recognition_day"] == "2026-09-16"
    assert item["opening"] == 100
    assert item["net_capital"] == -20
    assert item["effective_end"] == LAST
    output = derive.Output()
    with localcontext() as context:
        context.prec = numerics.PRECISION
        sleeves.emit_epoch(item, output, CUTOFF, ())
    metrics = {r["metric_id"]: r for r in output.metrics}
    assert metrics["flows.account.net"]["value"] == "-120.00"
    assert metrics["flows.account.count"]["value"] == "2"
    assert metrics["twr.account"]["value"] == "0.200000000000"
    assert metrics["twr.account"]["window"] == derive.WINDOW
    assert metrics["twr.account"]["as_of"] == LAST
    assert metrics["capital.epoch.opening"]["tolerance"] == "0.01"
    assert metrics["capital.epoch.opening"]["window"] == ""
    assert all(r["entity"] == "qb-crisis" for r in output.metrics)


def test_universe_rules_use_all_baseline_rows_and_same_parent() -> None:
    crosswalk = {
        "rows": [
            {
                "account_id": "ib",
                "strategy_id": "replaced",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "qb",
                "strategy_id": "live",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "different",
                "strategy_id": "retained",
                "product_epoch_disposition": "EXCLUDED",
            },
        ]
    }
    epochs = sleeves.ibkr_epochs(ib_facts(), ib_tag()) + sleeves.quantbase_epochs(
        qb_facts(), qb_tag()
    )
    result = sleeves.universe(crosswalk, [ib_tag(), qb_tag()], epochs)
    assert result["universe.residual_parent_ids"] == ["ib"]
    assert result["universe.retained_strategy_ids"] == []
    assert result["universe.registry_strategy_count"] == "3"
    crosswalk["rows"].pop()
    result = sleeves.universe(crosswalk, [ib_tag(), qb_tag()], epochs)
    assert result["universe.retained_strategy_ids"] == ["retained"]
    assert result["universe.retained_strategy_count"] == "1"


def manifest_tree(root: Path) -> Path:
    documents: dict[str, Any] = {role: {} for role in sleeves.ROLES}
    documents.update(
        {
            "crosswalk": {
                "rows": [
                    {
                        "account_id": "ib",
                        "strategy_id": "replaced",
                        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                    }
                ]
            },
            "tag_map": {"parents": [ib_tag(), qb_tag()]},
            "ibkr_catalog": {
                "catalog_schema_version": 7,
                "transactions": [],
                "positions": [],
                "change_in_nav_observations": [],
                "corporate_actions": [],
                "accounts": [],
            },
            "ibkr_declared_inputs": {
                "parent_account_id": "ib",
                "window": {"first_day": FIRST, "last_day": LAST},
            },
            "quantbase_declared_inputs": {
                "parent_account_id": "qb",
                "data_cutoff": LAST,
            },
        }
    )
    entries = {}
    for role, document in sorted(documents.items()):
        data = json.dumps(document, indent=2).replace("\n", "\r\n").encode()
        (root / f"{role}.json").write_bytes(data)
        entries[role] = {
            "root": "repo",
            "path": f"{role}.json",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {"schema": "qe-cp-sleeve-inputs/1", "cutoff": LAST, "inputs": entries}
        ),
        encoding="utf-8",
    )
    return path


def replace_input(path: Path, role: str, document: Any) -> None:
    data = json.dumps(document).encode()
    manifest = json.loads(path.read_bytes())
    (path.parent / manifest["inputs"][role]["path"]).write_bytes(data)
    manifest["inputs"][role]["sha256"] = hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "failure",
    ["bad_pin", "wrong_hash", "missing", "unknown", "cutoff", "duplicate", "escape"],
)
def test_manifest_errors(tmp_path: Path, failure: str) -> None:
    path = manifest_tree(tmp_path)
    value = json.loads(path.read_bytes())
    if failure == "bad_pin":
        value["inputs"]["crosswalk"]["sha256"] = "bad"
    if failure == "wrong_hash":
        value["inputs"]["crosswalk"]["sha256"] = "0" * 64
    if failure == "missing":
        del value["inputs"]["crosswalk"]
    if failure == "unknown":
        value["inputs"]["surprise"] = value["inputs"]["crosswalk"]
    if failure == "cutoff":
        value["cutoff"] = FIRST
    if failure == "escape":
        value["inputs"]["crosswalk"]["path"] = "../outside.json"
    text = json.dumps(value)
    if failure == "duplicate":
        text = text.replace('"inputs": {', '"inputs": {"crosswalk": {}, ', 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(InputError):
        sleeves.load_sleeve_inputs(path, tmp_path, tmp_path, CUTOFF)


@pytest.mark.parametrize("failure", ["ib_cutoff", "qb_cutoff", "catalog", "parent"])
def test_cli_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    path = manifest_tree(tmp_path)
    if failure == "ib_cutoff":
        replace_input(path, "ibkr_declared_inputs", {"window": {"last_day": FIRST}})
    if failure == "qb_cutoff":
        replace_input(path, "quantbase_declared_inputs", {"data_cutoff": FIRST})
    if failure == "catalog":
        replace_input(path, "ibkr_catalog", {"catalog_schema_version": 6})
    if failure == "parent":
        replace_input(path, "tag_map", {"parents": [ib_tag(), ib_tag(), qb_tag()]})
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    out = tmp_path / "out.json"
    assert (
        cli.main(
            [
                "--qe-root",
                str(tmp_path),
                "--out",
                str(out),
                "--sleeve-inputs",
                str(path),
                "--sleeves-only",
            ]
        )
        == 2
    )
    assert "input error:" in capsys.readouterr().err
    assert not out.exists()


def test_cli_requires_inputs_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Git must not be called")

    monkeypatch.setattr(cli, "_head_commit", forbidden)
    assert (
        cli.main(
            [
                "--qe-root",
                str(tmp_path),
                "--out",
                str(tmp_path / "out"),
                "--sleeves-only",
            ]
        )
        == 2
    )
    assert "input error:" in capsys.readouterr().err


def patch_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeves, "ibkr_facts", lambda *args: ib_facts())
    monkeypatch.setattr(sleeves, "quantbase_facts", lambda *args: qb_facts())


def test_cli_determinism_and_sleeves_only_skips_old_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = manifest_tree(tmp_path)
    patch_facts(monkeypatch)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Old inputs and Git must not be read")

    monkeypatch.setattr(derive, "_derive", forbidden)
    monkeypatch.setattr(cli, "_head_commit", forbidden)
    outputs = []
    for n in range(2):
        target = tmp_path / f"out{n}.json"
        assert (
            cli.main(
                [
                    "--qe-root",
                    str(tmp_path),
                    "--out",
                    str(target),
                    "--sleeve-inputs",
                    str(path),
                    "--sleeves-only",
                ]
            )
            == 0
        )
        outputs.append(target.read_bytes())
    assert outputs[0] == outputs[1]
    document = json.loads(outputs[0])
    assert len(document["metrics"]) == 24
    assert document["sleeve_derivation"]["schema"] == "qe-cp-sleeve-derivation/1"
    assert len(document["sleeve_derivation"]["epochs"]) == 2
    assert not document["not_derivable"]


def test_append_removes_only_residual_parent_window_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = manifest_tree(tmp_path)
    patch_facts(monkeypatch)
    rows = [
        {"metric_id": metric, "entity": account, "value": "old"}
        for metric in sorted(sleeves.ACCOUNT_METRICS | {"capital.account.cutoff"})
        for account in ["ib", "qb"]
    ]
    document = {"metrics": copy.deepcopy(rows), "not_derivable": copy.deepcopy(rows)}
    expected = [
        r
        for r in rows
        if r["entity"] != "ib" or r["metric_id"] == "capital.account.cutoff"
    ]
    sleeves.append_sleeves(document, path, tmp_path, tmp_path, CUTOFF)
    assert document["metrics"][: len(expected)] == expected
    assert document["not_derivable"] == expected


def test_no_sleeve_inputs_unchanged(tmp_path: Path) -> None:
    original._synthetic_root(tmp_path)
    cutoff = original.day(original.CUTOFF)
    with localcontext() as context:
        context.prec = numerics.PRECISION
        expected = derive._derive(
            tmp_path,
            original.REPOSITORY_ROOT,
            cutoff,
            "test",
            "test",
            {},
            original.LAYOUT,
            cutoff,
            None,
        )
    actual = derive.derive(
        tmp_path,
        original.REPOSITORY_ROOT,
        cutoff=cutoff,
        model="test",
        code_commit="test",
        expected_sha256={},
        layout=original.LAYOUT,
        declared_cutoff=cutoff,
    )
    assert derive.dumps(actual) == derive.dumps(expected)
    assert "sleeve_derivation" not in actual


def test_slice_dates_and_account_boundaries() -> None:
    catalog = {
        "catalog_schema_version": 7,
        "transactions": [
            {
                "account_id": "ib",
                "kind": "TRADE:BUY",
                "event_date": FIRST,
                "nav_recognition_date": "2026-10-01",
            },
            {
                "account_id": "ib",
                "kind": "CASH_ACTIVITY:Dividends",
                "event_date": "2026-09-01",
                "nav_recognition_date": FIRST,
            },
            {
                "account_id": "ib",
                "kind": "CASH_ACTIVITY:Dividends",
                "event_date": FIRST,
                "nav_recognition_date": "2026-10-01",
            },
            {"account_id": "other", "kind": "TRADE:BUY", "event_date": FIRST},
        ],
        "positions": [{"account_id": "ib", "as_of_date": FIRST}],
        "change_in_nav_observations": [{"account_id": "ib", "accounting_date": LAST}],
        "corporate_actions": [{"account_id": "ib", "date": "1900-01-01"}],
        "accounts": [{"account_id": "ib"}, {"account_id": "other"}],
    }
    result = sleeves.slice_ibkr(catalog, "ib", FIRST, LAST)
    assert result["transactions"] == catalog["transactions"][:2]
    assert result["corporate_actions"] == catalog["corporate_actions"]
    assert result["accounts"] == [{"account_id": "ib"}]


def test_engine_undefined_reason_is_preserved() -> None:
    item = sleeves.epoch(
        "toy",
        "strategy",
        {"2026-09-14": D(100), "2026-09-15": D(-1), LAST: D(10)},
        None,
        [],
    )
    output = derive.Output()
    with localcontext() as context:
        context.prec = numerics.PRECISION
        sleeves.emit_epoch(item, output, CUTOFF, ())
    (missing,) = [r for r in output.not_derivable if r["metric_id"] == "twr.account"]
    assert "UndefinedReturnError: beginning capital -1" in missing["reason"]
    assert any(
        r["metric_id"] == "mwr.account" and "InvalidOperation" in r["reason"]
        for r in output.not_derivable
    )
    assert any(r["metric_id"] == "flows.account.count" for r in output.metrics)


def test_nonpositive_first_row_is_not_skipped() -> None:
    item = sleeves.epoch("toy", "strategy", {FIRST: D(0), LAST: D(10)}, None, [])
    output = derive.Output()
    with localcontext() as context:
        context.prec = numerics.PRECISION
        sleeves.emit_epoch(item, output, CUTOFF, ())
    missing = {r["metric_id"]: r for r in output.not_derivable}
    assert "R3 TWR undefined (fail closed)" in missing["twr.account"]["reason"]
    assert "XIRR" in missing["mwr.account"]["reason"]
    assert item["opening"] == 0


def test_full_derive_hook_is_additive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = manifest_tree(tmp_path)
    patch_facts(monkeypatch)
    old = {
        "schema": "qe-cp-derivation/1",
        "metrics": [
            {"metric_id": "capital.account.cutoff", "entity": "ib", "value": "keep"},
            {"metric_id": "twr.account", "entity": "ib", "value": "replace"},
        ],
        "not_derivable": [],
    }
    monkeypatch.setattr(derive, "_derive", lambda *args: copy.deepcopy(old))
    result = derive.derive(
        tmp_path,
        tmp_path,
        cutoff=CUTOFF,
        model="test",
        code_commit="test",
        sleeve_inputs=path,
    )
    assert result["metrics"][0] == old["metrics"][0]
    assert old["metrics"][1] not in result["metrics"]
    assert len(result["sleeve_derivation"]["epochs"]) == 2
