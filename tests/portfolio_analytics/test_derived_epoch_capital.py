"""Governed invested capital of a derived sleeve epoch (R2, #567).

A residual parent's strategy epoch is replaced by the derived product epochs of
its hash-bound sleeve facts.  The sleeve's capital is the gross allocation into
it, taken from the facts' declared funding events and REALLOCATION legs (each
carrying the facts' ``evidence_ref``); the parent remainder stays residual, so
nothing is counted twice, and the capital reconciliation closes by construction
over exactly the manifest's product rows.  All inputs are synthetic.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import portfolio_financial_mvp_contract as financial
import portfolio_invested_capital as capital
import portfolio_totals as totals

REPO = Path(__file__).parents[2]
CONTRACT_PATH = REPO / "contracts" / "portfolio-financial-mvp-source-v1.json"
REGISTRY_PATH = REPO / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
BUILDER = REPO / "ops" / "mvp2-release" / "scripts" / "build_capital_input.py"
CUTOFF = "2026-09-17"
GENERATION = "c" * 64
PARENT = "ibkr-p123-ai-01"
RETAINED = "p123-conn-1911998-andreas-sc-ai-mq-live"
PRODUCT = "p123-ai-factor"
S1, S2 = f"{PARENT}-s1", f"{PARENT}-s2"
POOL = f"{PARENT}-unallocated"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _day_row(
    day: str, nav: str, *, event: str = "", alloc_total: str = "0"
) -> dict[str, Any]:
    return {
        "day": day,
        "event": event,
        "nav_end": nav,
        "nav_end_after_alloc": nav,
        "alloc_total": alloc_total,
    }


def _leg(
    pair: str, account: str, flow_class: str, day: str, amount: str
) -> dict[str, Any]:
    return {
        "pair_id": pair,
        "account_id": account,
        "class": flow_class,
        "flow_type": "TRANSFER:INTERNAL:SLEEVE",
        "event_date": day,
        "nav_recognition_date": day,
        "amount": amount,
    }


def _pair(
    pair: str, out_account: str, in_account: str, day: str, amount: str
) -> list[dict[str, Any]]:
    return [
        _leg(pair, out_account, "INTERNAL_OUT", day, amount),
        _leg(pair, in_account, "INTERNAL_IN", day, amount),
    ]


def sleeve_facts() -> dict[str, Any]:
    """s1 (history) funds 1000 and later hands 100 to s2; s2 is the product epoch.

    s2 receives 500 from the unallocated cash on its first day, 100 from s1 on
    2026-02-03 (a REALLOCATION) and gives 30 back on 2026-02-05.
    """

    flows = [
        *_pair("2026-01-26:0001", POOL, S1, "2026-01-26", "1000"),
        *_pair("2026-01-30:0002", POOL, S2, "2026-01-30", "500"),
        *_pair("2026-02-03:0003", S1, S2, "2026-02-03", "100"),
        *_pair("2026-02-05:0004", S2, POOL, "2026-02-05", "30"),
    ]
    return {
        "schema_version": 1,
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": PARENT,
        "provider_id": "IBKR",
        "base_currency": "USD",
        "window": {"first_day": "2026-01-26", "last_day": CUTOFF},
        "bindings": {
            "tag_map_sha256": _sha("tag"),
            "parent_ledger_generation_id": GENERATION,
            "declared_inputs_sha256": _sha("declared"),
            "account_rows_sha256": _sha("rows"),
            "builder_code": {
                "files": {"tools/a.py": _sha("a")},
                "same_tie_break": "x",
                "sha256": _sha("code"),
            },
        },
        "closure_policy": {
            "tolerance_per_day": "0.01",
            "tolerance_window": "0.02",
            "max_abs_unexplained": "0.01",
            "window_unexplained": "0.01",
        },
        "sleeves": [
            {
                "sleeve_id": "s1",
                "account_id": S1,
                "strategy_id": RETAINED,
                "first_day": "2026-01-26",
                "last_day": "2026-06-23",
                "days": [
                    _day_row("2026-01-26", "1000"),
                    _day_row("2026-06-23", "0", event="EXIT"),
                ],
            },
            {
                "sleeve_id": "s2",
                "account_id": S2,
                "strategy_id": PRODUCT,
                "first_day": "2026-01-30",
                "last_day": CUTOFF,
                "days": [
                    _day_row("2026-01-30", "500"),
                    _day_row(CUTOFF, "640"),
                ],
            },
        ],
        "perimeter_account": {
            "account_id": POOL,
            "opening_nav": "0",
            "days": [{"day": "2026-01-26", "nav_end": "0"}],
        },
        "flows": flows,
        "allocated_cost": {},
        "closure": [],
        "disclosures": {},
    }


def facts_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _census() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_bytes())["current_census"]


def _required_strategies() -> set[str]:
    contract = json.loads(CONTRACT_PATH.read_bytes())
    return {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    }


def _baseline_entries() -> list[tuple[str, dict[str, Any]]]:
    required = _required_strategies()
    return [
        (sleeve["sleeve_alias"], assignment)
        for sleeve in _census()["account_sleeves"]
        for assignment in sleeve["assignments"]
        if assignment["strategy_id"] in required
        and assignment.get("authority") != financial.DERIVED_SLEEVE_EPOCH_AUTHORITY
    ]


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("derived_capital_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest_rows(*, with_sleeves: bool) -> list[dict[str, Any]]:
    rows = []
    for alias, assignment in _baseline_entries():
        if with_sleeves and alias == PARENT:
            continue
        rows.append(
            {
                "account_id": alias,
                "strategy_id": assignment["strategy_id"],
                "effective_start": assignment["effective_start"],
                "effective_end": assignment["effective_end"],
            }
        )
    if with_sleeves:
        rows.append(
            {
                "account_id": S1,
                "strategy_id": RETAINED,
                "effective_start": "2026-01-26",
                "effective_end": "2026-06-23",
            }
        )
        rows.append(
            {
                "account_id": S2,
                "strategy_id": PRODUCT,
                "effective_start": "2026-01-30",
                "effective_end": None,
            }
        )
    return rows


def _day_before(day: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def build_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_sleeves: bool,
    facts: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """A provider ledger, epoch manifest and sleeve facts the real builder reads."""

    ledger_root = tmp_path / "ledger" / GENERATION
    ledger_root.mkdir(parents=True)
    epoch_root = tmp_path / "stages" / "epoch" / "generations" / ("e" * 64)
    (epoch_root / "outputs").mkdir(parents=True)
    flows = []
    admissions = []
    for alias, assignment in _baseline_entries():
        flows.append(
            {
                "record_id": f"deposit-{alias}-{assignment['effective_start']}",
                "provider_id": "IBKR",
                "account_id": alias,
                "strategy_id": None,
                "flow_type": "DEPOSIT_OR_WITHDRAWAL",
                "provider_account_scope": "ACCOUNT_ONLY",
                "capital_disposition": "EXTERNAL_CAPITAL",
                "event_date": _day_before(assignment["effective_start"]),
                "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
            }
        )
    for alias in {alias for alias, _ in _baseline_entries()} | {PARENT}:
        admissions.append(
            {
                "provider_id": "IBKR",
                "account_id": alias,
                "fact_type": "flows",
                "disposition": "AUTHORITATIVE",
                "effective_start": "2024-01-01",
                "effective_end": CUTOFF,
            }
        )
    ledger = {
        "capital_anchors": [],
        "flows": flows,
        "admissions": admissions,
        "nav_observations": [],
        "provider_generations": [
            {"provider_id": "ibkr", "generation_id": GENERATION},
        ],
    }
    ledger_path = ledger_root / "provider-ledger.json"
    ledger_path.write_bytes(capital._canonical(ledger))
    manifest = {
        "calculation_cutoff_date": CUTOFF,
        "rows": _manifest_rows(with_sleeves=with_sleeves),
    }
    (epoch_root / "outputs" / "account-strategy-manifest.json").write_bytes(
        capital._canonical(manifest)
    )
    provenance = {
        "as_of_date": CUTOFF,
        "accounts": [
            {"account_id": alias}
            for alias in sorted({a for a, _ in _baseline_entries()})
        ],
        "assignments": [
            {
                "account_id": alias,
                "strategy_id": assignment["strategy_id"],
                "effective_start": assignment["effective_start"],
                "effective_end": assignment["effective_end"],
                "mapping_status": "MAPPED",
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
            for alias, assignment in _baseline_entries()
        ],
    }
    provenance_path = tmp_path / "portfolio-provenance.json"
    provenance_path.write_bytes(capital._canonical(provenance))
    spec_path = tmp_path / "provider-ledger-spec.json"
    spec_path.write_bytes(
        capital._canonical(
            {
                "provenance": {
                    "catalog_path": str(provenance_path),
                    "catalog_sha256": capital._file_binding(provenance_path)[0][
                        "sha256"
                    ],
                    "generation_id": "a" * 64,
                }
            }
        )
    )
    crosswalk_path = tmp_path / "account-crosswalk.json"
    crosswalk_path.write_bytes(
        capital._canonical(
            {
                "bindings": [
                    {"sleeve_alias": alias, "account_id": alias}
                    for alias in sorted({a for a, _ in _baseline_entries()})
                ]
            }
        )
    )
    facts_path = tmp_path / "sleeve-facts.json"
    facts_path.write_bytes(facts_bytes(facts or sleeve_facts()))

    fake_bundle = SimpleNamespace(
        manifest=SimpleNamespace(verification=SimpleNamespace(value="VERIFIED"))
    )
    import quant_engine.core.publication as publication
    import portfolio_hwm_selected_inputs as selected

    monkeypatch.setattr(publication, "read_generation_bundle", lambda *_a: fake_bundle)
    monkeypatch.setattr(selected, "_source_generation_from_path", lambda *_a: None)
    import provider_ledger
    import portfolio_pipeline_materialize as materialize

    monkeypatch.setattr(
        provider_ledger,
        "verify_candidate",
        lambda *_a, **_k: {"state": "VERIFIED_STAGED"},
    )
    monkeypatch.setattr(
        materialize, "_provider_admission_authority", lambda *_a, **_k: None
    )
    builder = _load_builder()
    kwargs = {
        "provider_ledger_generation_root": ledger_root,
        "epoch_manifest_generation_root": epoch_root,
        "provider_ledger_spec_path": spec_path,
        "financial_contract_path": CONTRACT_PATH,
        "strategy_registry_path": REGISTRY_PATH,
        "account_crosswalk_path": crosswalk_path,
        "requested_as_of": CUTOFF,
        "evaluated_on": CUTOFF,
    }
    if with_sleeves:
        kwargs["sleeve_facts_paths"] = (facts_path,)
    generation, locators = builder.build_input_generation(**kwargs)
    input_path = tmp_path / "capital-input-generation.json"
    input_path.write_bytes(capital._canonical(generation))
    capital.write_local_sidecar(input_path, locators)
    return SimpleNamespace(
        generation=generation,
        locators=locators,
        input_path=input_path,
        facts_path=facts_path,
        ledger_path=ledger_path,
        manifest=manifest,
    )


def _result(world: SimpleNamespace) -> dict[str, Any]:
    result = capital.build_governed_capital_result(world.input_path, CONTRACT_PATH)
    assert capital.verify_governed_capital_result(
        result, world.input_path, CONTRACT_PATH
    )
    return result


def test_derived_product_epoch_capital_is_the_gross_allocation(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch, with_sleeves=True)
    result = _result(world)

    keys = {
        (row["account_id"], row["strategy_id"]): row
        for row in result["required_epochs"]
    }
    derived = keys[(S2, PRODUCT)]
    assert derived["sleeve_alias"] == "ibkr-p123-s2-01"
    assert derived["derivation"] == {
        "kind": "DERIVED_SLEEVE",
        "parent_account_id": PARENT,
        "sleeve_facts_sha256": hashlib.sha256(
            world.facts_path.read_bytes()
        ).hexdigest(),
    }
    # the retired sleeve is history: no governed capital; the parent is residual
    assert not any(
        row["account_id"] in {S1, PARENT} for row in result["required_epochs"]
    )
    by_strategy = {
        row["strategy_id"]: row["invested_capital_usd"]
        for row in result["current_by_strategy"]
    }
    # 500 from the unallocated cash + 100 reallocated from s1 - 30 returned
    assert by_strategy[PRODUCT] == "570"
    events = [e for e in result["events"] if e["epoch_id"] == derived["epoch_id"]]
    assert sorted((e["date"], e["classification"], e["amount"]) for e in events) == [
        ("2026-01-30", "EXTERNAL_CONTRIBUTION", "500"),
        ("2026-01-30", "OPENING_CONTRIBUTED_BASIS", "0"),
        ("2026-02-03", "EXTERNAL_CONTRIBUTION", "100"),
        ("2026-02-05", "EXTERNAL_WITHDRAWAL", "30"),
    ]
    # each allocation cites the facts' own leg, not a NAV
    assert all(
        e["source_evidence_sha256"] != e["epoch_id"]
        for e in events
        if e["classification"] != "OPENING_CONTRIBUTED_BASIS"
    )
    sources = {k for k in result["source_bindings"] if k.startswith("sleeve_facts:")}
    assert sources == {
        capital.sleeve_facts_role(derived["derivation"]["sleeve_facts_sha256"])
    }
    # the parent's own deposits are not capital: nothing is counted twice
    assert result["current_aggregate_usd"] == str(
        sum(Decimal(v) for v in by_strategy.values())
    )


def test_totals_reconcile_over_exactly_the_product_rows(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch, with_sleeves=True)
    result = _result(world)

    rows = []
    for epoch in result["required_epochs"]:
        governed = sum(
            Decimal(e["invested_capital_effect_usd"])
            for e in result["events"]
            if e["epoch_id"] == epoch["epoch_id"]
            and e["classification"] != "PREDECESSOR_TRANSITION"
        )
        opening = governed + Decimal("7")  # NAV basis differs from governed capital
        end = epoch["effective_end"]
        current = opening + Decimal("11")
        rows.append(
            {
                "account_id": epoch["account_id"],
                "strategy_id": epoch["strategy_id"],
                "effective_start": epoch["effective_start"],
                "effective_end": end,
                "start": epoch["effective_start"],
                "end": end or CUTOFF,
                "status": "OK",
                "opening_basis": "FIRST_AVAILABLE_NAV",
                "opening_capital": str(opening),
                "deposits": "0",
                "internal_in": "0",
                "external_withdrawals": "0",
                "internal_out": "0",
                "net_capital": str(opening),
                "current_value": str(current),
                "gain": "11",
                "observation_trace": {"links": []},
            }
        )
    block = totals.build_capital_reconciliation(rows, result, CUTOFF)
    assert block["status"] == "RECONCILED"
    assert block["residual_usd"] == "0"
    assert block["epoch_count"] == len(result["required_epochs"])
    # a manifest row set that still carried the parent's epoch is named, not hidden
    stale = deepcopy(rows)
    stale[0]["account_id"] = PARENT
    assert (
        totals.build_capital_reconciliation(stale, result, CUTOFF)["reason"]
        == "EPOCH_ROWS_DIFFER_FROM_GOVERNED_EPOCHS"
    )


def test_a_world_without_sleeves_is_unchanged(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch, with_sleeves=False)
    generation = world.generation
    assert not any(
        role.startswith("sleeve_facts:") for role in generation["source_bindings"]
    )
    assert not any("derivation" in row for row in generation["required_epochs"])
    assert set(world.locators) == {
        "provider_ledger",
        "provider_ledger_spec",
        "account_crosswalk",
        "strategy_registry",
    }
    result = _result(world)
    assert PARENT in {row["account_id"] for row in result["required_epochs"]}
    assert len(result["required_epochs"]) == 13


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("drop_derived_epoch", "derived required epochs differ"),
        ("forge_derivation_on_baseline", "derived required epochs differ"),
        ("keep_parent_epoch", "residual parent keeps a baseline epoch"),
        ("drop_other_baseline", "not a residual parent"),
        ("unbind_facts", "exact required epoch identities differ"),
        ("alter_amount", "admitted capital source amount differs"),
        ("drop_event", "selected capital flow is omitted"),
    ],
)
def test_a_tampered_generation_is_refused(tmp_path, monkeypatch, tamper, message):
    world = build_world(tmp_path, monkeypatch, with_sleeves=True)
    generation = deepcopy(world.generation)
    derived = next(row for row in generation["required_epochs"] if "derivation" in row)
    baseline = next(
        row for row in generation["required_epochs"] if "derivation" not in row
    )
    if tamper == "drop_derived_epoch":
        generation["required_epochs"].remove(derived)
    elif tamper == "forge_derivation_on_baseline":
        baseline["derivation"] = deepcopy(derived["derivation"])
    elif tamper == "keep_parent_epoch":
        generation["required_epochs"].append(
            {
                "account_id": PARENT,
                "effective_end": None,
                "effective_start": "2026-01-22",
                "epoch_id": f"ibkr-p123-ai-01|{PRODUCT}|2026-01-22",
                "sleeve_alias": "ibkr-p123-ai-01",
                "strategy_id": PRODUCT,
            }
        )
    elif tamper == "drop_other_baseline":
        # an account that is not a parent of any sleeve cannot lose its epoch
        generation["required_epochs"].remove(baseline)
        generation["events"] = [
            e for e in generation["events"] if e["epoch_id"] != baseline["epoch_id"]
        ]
        generation["opening_basis"] = [
            e
            for e in generation["opening_basis"]
            if e["epoch_id"] != baseline["epoch_id"]
        ]
        generation["flow_coverage"] = [
            e
            for e in generation["flow_coverage"]
            if e["epoch_id"] != baseline["epoch_id"]
        ]
    elif tamper == "unbind_facts":
        generation["source_bindings"] = {
            k: v
            for k, v in generation["source_bindings"].items()
            if not k.startswith("sleeve_facts:")
        }
    elif tamper == "alter_amount":
        event = next(
            e
            for e in generation["events"]
            if e["epoch_id"] == derived["epoch_id"]
            and e["classification"] == "EXTERNAL_CONTRIBUTION"
        )
        event["amount"] = "501"
    else:
        generation["events"] = [
            e
            for e in generation["events"]
            if not (e["epoch_id"] == derived["epoch_id"] and e["date"] == "2026-02-03")
        ]
    world.input_path.write_bytes(capital._canonical(generation))
    with pytest.raises(capital.CapitalValidationError, match=message):
        capital.build_governed_capital_result(world.input_path, CONTRACT_PATH)


def test_facts_bound_to_another_ledger_generation_are_refused(tmp_path, monkeypatch):
    facts = sleeve_facts()
    facts["bindings"]["parent_ledger_generation_id"] = "d" * 64
    with pytest.raises(capital.CapitalValidationError, match="generation"):
        build_world(tmp_path, monkeypatch, with_sleeves=True, facts=facts)


def test_outcomes_bind_capital_to_the_selected_facts(tmp_path, monkeypatch):
    import portfolio_financial_outcomes as outcomes

    world = build_world(tmp_path, monkeypatch, with_sleeves=True)
    outcomes.verify_selected_capital_sleeve_facts(world.input_path, (world.facts_path,))
    with pytest.raises(outcomes.FinancialOutcomeError, match="sleeve facts differ"):
        outcomes.verify_selected_capital_sleeve_facts(world.input_path, ())
    other = tmp_path / "other-facts.json"
    other.write_bytes(facts_bytes({**sleeve_facts(), "window": {"first_day": "x"}}))
    with pytest.raises(outcomes.FinancialOutcomeError, match="sleeve facts differ"):
        outcomes.verify_selected_capital_sleeve_facts(world.input_path, (other,))


QB_PARENT = "qb-account-01"
QB_GENERATION = "b" * 64


def quantbase_facts() -> dict[str, Any]:
    """A live-parent (fund-scoped) account whose retired fund hands its value on.

    The live fund's leg is a PARENT_LEDGER_REFERENCE: the parent ledger already
    carries it, so it is a pairing reference and never a second flow.
    """

    pool = f"{QB_PARENT}-unallocated"
    fund = f"{QB_PARENT}-single"
    legs = [
        {
            **_leg("2024-07-24:0001", pool, "INTERNAL_OUT", "2024-07-24", "500"),
            "leg_source": "ARTIFACT",
        },
        {
            **_leg("2024-07-24:0001", fund, "INTERNAL_IN", "2024-07-24", "500"),
            "leg_source": "ARTIFACT",
        },
        {
            **_leg("2025-05-05:0002", fund, "INTERNAL_OUT", "2025-05-05", "480"),
            "leg_source": "ARTIFACT",
        },
        {
            **_leg("2025-05-05:0002", QB_PARENT, "INTERNAL_IN", "2025-05-05", "480"),
            "leg_source": "PARENT_LEDGER_REFERENCE",
        },
    ]
    return {
        "schema_version": 1,
        "artifact_id": "quantbase-fund-facts",
        "parent_account_id": QB_PARENT,
        "provider_id": "QUANTBASE",
        "base_currency": "USD",
        "data_cutoff": "2025-05-06",
        "window": {"first_day": "2024-07-24", "last_day": "2025-05-06"},
        "bindings": {
            "tag_map_sha256": _sha("tag"),
            "parent_ledger_generation_id": QB_GENERATION,
            "declared_inputs_sha256": _sha("declared"),
            "source_sha256": {
                "account_activity": _sha("a"),
                "accounts": _sha("b"),
                "dashboards": {"single": _sha("c")},
            },
            "builder_code": {
                "files": {"tools/a.py": _sha("a")},
                "sha256": _sha("code"),
            },
        },
        "conventions": {},
        "sleeves": [
            {
                "sleeve_id": "single",
                "account_id": fund,
                "strategy_id": "quantbase-single-index-leveraged-in-out",
                "first_day": "2024-07-24",
                "last_day": "2025-05-06",
                "days": [
                    {"day": "2024-07-24", "event": "OPEN", "nav_end": "500"},
                    {"day": "2025-05-06", "event": "", "nav_end": "0"},
                ],
            }
        ],
        "perimeter_account": {"account_id": pool, "days": [], "scope": "x"},
        "flows": legs,
        "closure": {},
        "open_items": {},
        "disclosures": {},
    }


def test_quantbase_live_parent_facts_derive_retained_epochs_and_one_flow_pair() -> None:
    ledger = {
        "flows": [],
        "capital_anchors": [],
        "nav_observations": [],
        "admissions": [
            {"account_id": QB_PARENT, "provider_id": "quantbase", "fact_type": "flows"}
        ],
        "provider_generations": [
            {"provider_id": "quantbase", "generation_id": QB_GENERATION}
        ],
    }
    view = capital.derive_sleeve_capital(
        [facts_bytes(quantbase_facts())], ledger, _required_strategies()
    )
    fund = f"{QB_PARENT}-single"
    key = (
        fund,
        "quantbase-single-index-leveraged-in-out",
        "2024-07-24",
        "2025-05-06",
    )
    assert set(view.epochs) == {key}
    # a retired fund is history: no governed capital epoch
    assert view.product == frozenset()
    assert view.parents == {QB_PARENT}
    # the parent-ledger reference leg is not a flow; the artifact pair is
    assert sorted((row["account_id"], row["amount"]["value"]) for row in view.rows) == [
        (f"{QB_PARENT}-single", "-480"),
        (f"{QB_PARENT}-single", "500"),
        (f"{QB_PARENT}-unallocated", "-500"),
    ]
    assert {row["provider_id"] for row in view.rows} == {"QUANTBASE"}
    # a parent the ledger does not know is refused
    ledger["admissions"] = []
    with pytest.raises(capital.CapitalValidationError, match="selected ledger"):
        capital.derive_sleeve_capital(
            [facts_bytes(quantbase_facts())], ledger, _required_strategies()
        )
