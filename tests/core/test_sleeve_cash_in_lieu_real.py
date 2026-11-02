"""The cash in lieu of a corporate-action fraction, on the real IBKR bytes (env-gated).

Real data is never committed.  ``QE_AI624_R21_IBKR_CATALOG`` names the R2.1-lineage
IBKR catalog (p24, ``ibkr-catalog-48e7db0e.json``); ``QE_AI624_R2_IBKR_CATALOG`` the
R2 catalog (``ibkr-catalog-cacc1424.json``, same account rows, other ids);
``QE_AI624_R2_IBKR_ROOT`` the dataIBKR root that holds the saved Flex statements.
Each test skips when its input is absent.  The tag map and declared inputs are the
committed ones under ``ops/mvp2-release/inputs/sleeves``.

R2 regression references.  The R2 facts were built with the pro-rata rule: s2
return 0.154347647891, series sha 4243e026..., closing value 77258.1346867.  They are
kept below as the references the corrected rule is compared with, and nowhere else.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
import glob
import json
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import pytest

import build_sleeve_account_facts as builder
import nested_strategy_tag_map as tag_map_module
import sleeve_ledger as sl
import strategy_basis
import verify_sleeve_ledger as verifier

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_SLEEVES = _REPO / "ops" / "mvp2-release" / "inputs" / "sleeves"
_R21_CATALOG = os.environ.get("QE_AI624_R21_IBKR_CATALOG")
_R2_CATALOG = os.environ.get("QE_AI624_R2_IBKR_CATALOG")
_IBKR_ROOT = os.environ.get("QE_AI624_R2_IBKR_ROOT")

# R2 regression references (pro-rata rule, superseded)
_R2_SERIES_SHA_PREFIX = "4243e026"
_R2_RETURN = D("0.154347647891")
_R2_VALUE = D("77258.1346867")

needs_r21 = pytest.mark.skipif(
    not _R21_CATALOG or not Path(_R21_CATALOG or "").is_file(),
    reason="QE_AI624_R21_IBKR_CATALOG is not set to the R2.1 IBKR catalog",
)


def _declared() -> tuple[bytes, bytes, Any]:
    tag = (_SLEEVES / "nested-strategy-tag-map.json").read_bytes()
    declared = (_SLEEVES / "sleeve-declared-inputs.json").read_bytes()
    return tag, declared, json.loads(declared)


def _rows(catalog: str) -> tuple[builder.AccountRows, str, bytes, bytes, Any]:
    tag, declared_bytes, declared = _declared()
    tag_map = tag_map_module.validate_tag_map(tag)
    parent = tag_map.parent(declared["parent_account_id"])
    rows, sha = builder.rows_from_catalog(
        Path(catalog).read_bytes(),
        declared["parent_account_id"],
        parent.provider_id,
        declared["window"]["first_day"],
        declared["window"]["last_day"],
    )
    return rows, sha, tag, declared_bytes, declared


def _ledger(
    rows: builder.AccountRows, tag: bytes, declared_bytes: bytes
) -> tuple[sl.SleeveLedger, dict[str, dict[str, Decimal]]]:
    tag_map = tag_map_module.validate_tag_map(tag)
    declared = builder.load_declared_inputs(declared_bytes, tag_map)
    parent = tag_map.parent(declared.parent_account_id)
    with localcontext() as context:
        context.prec = 28
        _account, days = builder._account_days(rows, declared)
        inputs = builder.inputs_module.sleeve_inputs(
            rows.transactions,
            rows.positions,
            parent,
            base_currency=declared.base_currency,
            days=days,
            corporate_actions=rows.corporate_actions,
        )
        policy = inputs.policy(
            declared.residual_id,
            aliases=declared.aliases,
            alias_days=declared.alias_days,
        )
        policy = sl.SleevePolicy(
            policy.sleeve_ids,
            policy.residual_id,
            policy.aliases,
            policy.alias_from_day,
            policy.alias_days,
            {**dict(inputs.unheld_fallback), **dict(declared.unheld_fallback)},
        )
        ledger = sl.derive_sleeve_ledger(
            inputs.trades,
            inputs.positions,
            inputs.marks,
            inputs.dividends,
            inputs.resolve,
            policy,
            inputs.corporate_actions,
        )
    return ledger, dict(inputs.positions)


def _facts(catalog: str) -> tuple[bytes, builder.AccountRows, str, bytes, bytes]:
    rows, sha, tag, declared_bytes, _declared_doc = _rows(catalog)
    artifact = builder.build_sleeve_account_facts(
        tag_map_bytes=tag,
        declared_inputs_bytes=declared_bytes,
        rows=rows,
        rows_sha256=sha,
        parent_ledger_generation_id="4becf8c279766c18ed6c9d8a844590cac7225c298a2fea8a50088bd2df4e565d",
    )
    return artifact, rows, sha, tag, declared_bytes


@needs_r21
def test_real_the_cash_in_lieu_is_split_by_own_entitlement_not_pro_rata() -> None:
    assert _R21_CATALOG is not None
    artifact, *_ = _facts(_R21_CATALOG)
    facts = json.loads(artifact)
    (block,) = facts["disclosures"]["corporate_action_fractions"]
    by_sleeve = {item["sleeve_id"]: item for item in block["sleeves"]}
    # the sleeves' own entitlements add up to the executed fraction and to whole shares
    assert D(block["closure"]["fraction_total"]) == D(block["closure"]["sold_quantity"])
    assert D(block["closure"]["whole_total"]) == D(block["closure"]["account_position"])
    for item in by_sleeve.values():
        assert D(item["whole_shares"]) == D(item["whole_shares"]).to_integral_value()
        assert D(item["entitlement"]) - D(item["fraction"]) == D(item["whole_shares"])
        assert D(item["charged_quantity"]) == -D(item["fraction"])
    assert {
        s: (i["held_quantity"], i["entitlement"], i["fraction"])
        for s, i in by_sleeve.items()
    } == {
        "s1": ("220", "27.5", "0.5"),
        "s2": ("130", "16.25", "0.25"),
    }
    assert facts["disclosures"]["offsetting_residues"] == []
    assert [
        item["how"]
        for item in facts["disclosures"]["manual_assignments"]
        if item["how"] == "split"
    ] == []


@needs_r21
def test_real_every_sleeve_holds_whole_shares_on_every_day_after_the_action() -> None:
    assert _R21_CATALOG is not None
    rows, _sha, tag, declared_bytes, _ = _rows(_R21_CATALOG)
    ledger, _positions = _ledger(rows, tag, declared_bytes)
    (record,) = ledger.cash_in_lieu
    checked = 0
    for day in sorted(ledger.holdings):
        if day < record.day:
            continue
        for sleeve in ("s1", "s2"):
            held = ledger.holdings[day].get(sleeve, {}).get(record.security_id, D(0))
            assert held == held.to_integral_value(), (day, sleeve, held)
        checked += 1
    assert checked > 100


@needs_r21
def test_real_no_other_untagged_execution_leaves_a_sleeve_with_a_fraction_the_account_lacks() -> (
    None
):
    """Every untagged execution closes: a sleeve holds a fraction only where the account does."""

    assert _R21_CATALOG is not None
    rows, _sha, tag, declared_bytes, _ = _rows(_R21_CATALOG)
    ledger, account = _ledger(rows, tag, declared_bytes)
    assert not [item for item in ledger.manual_assignments if item[3] == "split"]
    for day, book in ledger.holdings.items():
        totals: dict[str, Decimal] = {}
        for holdings in book.values():
            for symbol, quantity in holdings.items():
                totals[symbol] = totals.get(symbol, D(0)) + quantity
        for sleeve, holdings in book.items():
            for symbol, quantity in holdings.items():
                if quantity != quantity.to_integral_value():
                    held = account[day].get(symbol, D(0))
                    assert held != held.to_integral_value(), (day, sleeve, symbol)
                    assert abs(totals[symbol] - held) <= D("1e-6"), (day, symbol)


@needs_r21
def test_real_the_independent_verifier_reproduces_the_whole_rebuilt_artifact() -> None:
    assert _R21_CATALOG is not None
    artifact, _rows_, _sha, tag, declared_bytes = _facts(_R21_CATALOG)
    declared = json.loads(declared_bytes)
    chosen, sha = verifier.select_catalog_rows(
        Path(_R21_CATALOG).read_bytes(),
        declared["parent_account_id"],
        declared["window"]["first_day"],
        declared["window"]["last_day"],
    )
    report = verifier.verify_sleeve_account_facts(
        artifact, tag, declared_bytes, chosen, sha
    )
    assert report.mismatches == (), report.mismatches[:3]


@needs_r21
def test_real_the_sleeve_path_moves_by_the_corrected_split_only() -> None:
    assert _R21_CATALOG is not None
    artifact, *_ = _facts(_R21_CATALOG)
    derived = strategy_basis.restatement_from_sleeve_facts(artifact, "p123-ai-factor")
    assert len(derived.rows) == 164
    cumulative = D(1)
    for row in derived.rows:
        cumulative *= 1 + D(row["return"])
    cumulative -= 1
    # R2 (pro rata): 0.154347647891; corrected: about 0.15433337
    assert abs(cumulative - _R2_RETURN) < D("0.00002")
    assert cumulative != _R2_RETURN
    assert not strategy_basis.series_sha256(derived.rows).startswith(
        _R2_SERIES_SHA_PREFIX
    )
    assert strategy_basis.series_sha256(derived.rows).startswith("29c2b718")
    assert D("77258.0018") < derived.value < D("77258.0019")
    assert abs(derived.value - _R2_VALUE) < D("0.14")


@pytest.mark.skipif(
    not _R2_CATALOG or not Path(_R2_CATALOG or "").is_file(),
    reason="QE_AI624_R2_IBKR_CATALOG is not set to the R2 IBKR catalog",
)
def test_real_the_r2_catalog_gives_the_same_corrected_figures_as_r2_1() -> None:
    assert _R2_CATALOG is not None and _R21_CATALOG is not None
    if not Path(_R21_CATALOG).is_file():
        pytest.skip("QE_AI624_R21_IBKR_CATALOG is not set")
    one = json.loads(_facts(_R2_CATALOG)[0])
    two = json.loads(_facts(_R21_CATALOG)[0])
    assert one["sleeves"] == two["sleeves"]
    assert one["disclosures"] == two["disclosures"]
    assert (
        one["bindings"]["corporate_actions_sha256"]
        == two["bindings"]["corporate_actions_sha256"]
    )


@pytest.mark.skipif(
    not _IBKR_ROOT or not Path(_IBKR_ROOT or "").is_dir(),
    reason="QE_AI624_R2_IBKR_ROOT is not set to the dataIBKR root",
)
@needs_r21
def test_real_the_saved_flex_statement_states_the_execution_and_the_split() -> None:
    """The execution and the action are in the broker's own statement, independent of the catalog."""

    assert _IBKR_ROOT is not None and _R21_CATALOG is not None
    artifact, *_ = _facts(_R21_CATALOG)
    (block,) = json.loads(artifact)["disclosures"]["corporate_action_fractions"]
    account = os.environ.get("QE_AI624_R2_IBKR_ACCOUNT", "U22596907")
    trades: list[dict[str, str]] = []
    actions: list[dict[str, str]] = []
    pattern = "cutover-recovery/legacy-original-*/AllAccounts_Accounting_XML_2026*.xml"
    for path in sorted(glob.glob(str(Path(_IBKR_ROOT) / pattern))):
        for _event, element in ET.iterparse(path, events=("end",)):
            attrs = element.attrib
            if attrs.get("accountId") == account:
                if element.tag == "Trade" and attrs.get("tradeDate", "").replace(
                    "-", ""
                ) == block["day"].replace("-", ""):
                    trades.append(dict(attrs))
                if (
                    element.tag == "CorporateAction"
                    and attrs.get("type") == block["action"]["kind"]
                ):
                    actions.append(dict(attrs))
            element.clear()
    sold = [
        item
        for item in trades
        if D(item["quantity"]) == D(block["execution"]["quantity"])
        and D(item["tradePrice"]) == D(block["execution"]["price"])
    ]
    assert len(sold) == 1
    assert sold[0]["orderReference"] == ""  # untagged: the reason it needed a rule
    assert D(sold[0]["ibCommission"]) == D(block["execution"]["commission"])
    quantities = {D(item["quantity"]) for item in actions}
    assert D(block["action"]["new_quantity"]) in quantities
    assert -D(block["action"]["old_quantity"]) in quantities
